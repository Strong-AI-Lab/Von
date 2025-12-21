# LLM Workflows: Design Document

**Status**: Draft
**Version**: 1.0
**Date**: 2025-12-21
**JIRA**: [JVNAUTOSCI-803](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-803)
**Authors**: Von AI Assistant, Michael Witbrock

---

## Executive Summary

This document outlines the design for **LLM Workflows** — a compositional framework that transforms Von's currently ad-hoc LLM invocations into structured, observable, and reusable patterns. The key insight is that every LLM operation (chat, annotation, rumination, stored prompts) can be modelled as a workflow comprising one or more actions with explicit control flow.

**Core Benefits**:
- **Observability**: Complete execution traces for debugging and optimisation
- **Reusability**: Common patterns (retry, validation, caching) extracted once
- **Composability**: Build complex multi-step reasoning from simpler components
- **Security**: Unified access control and audit trail
- **Performance**: Systematic identification and elimination of bottlenecks

---

## Table of Contents

1. [Background & Context](#1-background--context)
2. [Current State Analysis](#2-current-state-analysis)
3. [Design Principles](#3-design-principles)
4. [Conceptual Model](#4-conceptual-model)
5. [Vontology Schema](#5-vontology-schema)
6. [Workflow Definition Language](#6-workflow-definition-language)
7. [Control Flow Patterns](#7-control-flow-patterns)
8. [Implementation Architecture](#8-implementation-architecture)
9. [Migration Strategy](#9-migration-strategy)
10. [Integration Points](#10-integration-points)
11. [Performance Considerations](#11-performance-considerations)
12. [Security & Access Control](#12-security--access-control)
13. [Testing Strategy](#13-testing-strategy)
14. [Operational Concerns](#14-operational-concerns)
15. [Future Extensions](#15-future-extensions)
16. [Glossary](#16-glossary)

---

## 1. Background & Context

### 1.1 Current State

Von uses large language models (LLMs) across multiple subsystems:

- **Chat Assistant**: User dialogue with tool invocation capabilities
- **Annotation Extraction**: Identifying concepts/entities in text
- **Stored Prompts**: Vontology-backed prompt templates for specific tasks
- **Rumination**: Proactive knowledge graph enrichment
- **Tell-Von Agent**: Natural language task inference

Each subsystem implements its own LLM invocation logic, leading to:
- Code duplication (retry logic, error handling, logging)
- Limited observability (no unified execution tracing)
- Difficult debugging (scattered LLM calls with inconsistent patterns)
- Missed optimisation opportunities (no systematic analysis of LLM usage)

### 1.2 Existing Vontology Concept

The Vontology already contains `#V#llm_action`:

> "An action performed by invoking a large language model with a specific prompt and model configuration. LLM actions represent discrete computational operations that use language models for analysis, generation, or decision-making."

**Key Instance**: `#V#detect_missing_tool_call_action` (used in chat orchestrator recovery)

This foundational concept provides the building block for workflows.

### 1.3 Motivation

**Observation**: Most LLM operations are not isolated actions but **sequential or conditional processes**:

- Chat: [build context] → [invoke LLM] → [parse tools] → [execute tools] → [format response]
- Annotation: [load prompt] → [invoke LLM] → [parse JSON] → [map types] → [rank candidates]
- Rumination: [identify gap] → [query LLM] → [validate] → [update graph] (iterate)

Making these implicit workflows **explicit** enables:
- Systematic analysis and optimisation
- Reusable patterns across subsystems
- Automatic instrumentation and monitoring
- Declarative specification (stored in Vontology)

---

## 2. Current State Analysis

### 2.1 LLM Invocation Inventory

#### Chat Assistant (`InternalMCPChatOrchestrator`)

**File**: `src/backend/integrations/internal_mcp/orchestrator.py`

**Current Flow**:
```python
def run(prompt, context, ...):
    1. Build system message (with tool instructions)
    2. Construct conversation history
    3. LOOP up to max_tool_invocations:
         a. Invoke LLM
         b. Extract tool calls from response
         c. IF tool calls present:
              - Execute via MCP gateway
              - Append results to context
         d. ELSE:
              - Return response
    4. Handle missing tool call detection (recovery)
```

**Characteristics**:
- **Control Flow**: Iterative with early exit
- **Error Handling**: Retry for missing tool calls
- **Observability**: Partial (logs exist, no structured trace)
- **LOC**: ~400 lines (orchestration + helpers)

**Workflow Opportunity**: This is already a rudimentary workflow — formalise it.

#### Annotation Extraction Service

**File**: `src/backend/services/annotation_extraction_service.py`

**Current Flow**:
```python
def extract_annotations(text):
    1. Load prompt from Vontology (#V#find_concepts_in_text_prompt)
    2. Build full prompt with text
    3. Invoke LLM
    4. Parse JSON response
       IF parse fails:
          - Attempt regex fallback extraction
    5. Map type labels to concept IDs
    6. Score and rank candidates
    7. Return annotation spans
```

**Characteristics**:
- **Control Flow**: Sequential with conditional fallback
- **Error Handling**: Graceful degradation (regex fallback)
- **Caching**: Prompt cached (TTL=300s), phrase cache for candidate matching
- **LOC**: ~500 lines (extraction + helpers)

**Workflow Opportunity**: Sequential workflow with error recovery step.

#### Stored Prompt Execution

**Files**: Various (referenced in `JVNAUTOSCI-797`)

**Current Pattern**:
- Prompts stored as Vontology concepts (via `hasContent` or `hasDescription`)
- Template expansion with user/org context
- Single LLM invocation
- No inherent retry or validation logic

**Workflow Opportunity**: Enable multi-step prompts (e.g., "analyse text, extract entities, validate against Vontology").

#### Rumination & Relation-Filling

**Context**: JIRA-371 (Proactive Relation-Filling), JIRA-377 (Annotation Rumination)

**Anticipated Pattern**:
- Identify missing relations for concept
- LOOP for each missing field:
    - Query LLM for plausible value
    - Validate against schema/constraints
    - IF valid: update knowledge graph
- Continue until all gaps filled or max iterations reached

**Workflow Opportunity**: Canonical iterative workflow with validation gates.

### 2.2 Common Patterns Identified

Across all subsystems, the following **workflow primitives** emerge:

1. **Sequential Execution**: Step A → Step B → Step C
2. **Conditional Branching**: IF condition THEN action_X ELSE action_Y
3. **Iteration**: WHILE condition DO action (with max iterations safeguard)
4. **Error Recovery**: TRY action CATCH error THEN fallback_action
5. **Parallel Fan-Out**: PARALLEL [action_A, action_B, action_C] THEN aggregate
6. **Caching**: Retrieve cached result if available, else compute and cache

---

## 3. Design Principles

### 3.1 Core Principles

1. **Explicit Over Implicit**: Every LLM operation should declare its workflow structure
2. **Composability**: Workflows can be composed of smaller workflows (fractal pattern)
3. **Declarative First**: Workflow definitions stored as data (Vontology), not just code
4. **Backward Compatible**: Existing code continues to work during migration
5. **Observable by Default**: Every workflow execution produces a structured trace
6. **Fail-Safe**: Failures at any step are captured and don't crash the system
7. **User-Scoped**: Workflows respect user/org permissions and context

### 3.2 Non-Goals (Out of Scope)

- **General Workflow Engine**: This is not a replacement for Airflow/Temporal; scope is LLM-centric
- **Visual Workflow Builder**: UI for workflow creation deferred to Phase 4
- **Real-Time Workflow Modification**: Runtime changes require explicit API (future feature)
- **Cross-System Orchestration**: External system integrations (beyond MCP) not covered

---

## 4. Conceptual Model

### 4.1 Workflow Hierarchy

```
LLMWorkflow
  ├─ metadata (id, name, description)
  ├─ control_flow_type (sequential | conditional | iterative | parallel | dag)
  ├─ steps: List[WorkflowStep]
  │    ├─ step_id
  │    ├─ action (reference to LLMAction or nested LLMWorkflow)
  │    ├─ condition (optional predicate)
  │    ├─ retry_policy
  │    └─ dependencies (for DAG flows)
  └─ failure_strategy (halt | continue | retry | fallback)
```

### 4.2 Workflow vs Action

**LLMAction**: Atomic unit — a single LLM invocation with specific prompt/model/params

**LLMWorkflow**: Composition of actions (or sub-workflows) with control flow logic

**Relationship**: Every LLMWorkflow contains ≥1 LLMAction. An LLMAction can exist standalone or within a workflow.

### 4.3 Execution Model

```
WorkflowEngine.execute_workflow(workflow_id, inputs, context) -> WorkflowResult
  ├─ Load workflow definition from Vontology
  ├─ Initialise workflow state (inputs + context)
  ├─ FOR each step in workflow:
  │    ├─ Evaluate step condition (if present)
  │    ├─ IF condition met:
  │    │    ├─ Execute step action (dispatch to executor)
  │    │    ├─ Handle retries if step fails
  │    │    ├─ Capture step result in trace
  │    │    └─ Update workflow state
  ├─ Apply control flow logic (iteration, branching, etc.)
  └─ Return WorkflowResult (output + trace + metadata)
```

---

## 5. Vontology Schema

### 5.1 Core Concepts

#### `#V#llm_workflow` (Type)

**Parent**: `#V#workflow` (create if doesn't exist, else `#V#action`)

**Description**: A compositional pattern comprising one or more LLM actions with defined sequencing and control flow.

**Attributes** (via text relations):
- **hasDefinition** (JSON):
  ```json
  {
    "control_flow": "sequential",
    "failure_strategy": "halt",
    "max_iterations": null,
    "timeout_seconds": 300,
    "steps": [...]
  }
  ```
- **hasDescription**: Human-readable explanation of workflow purpose
- **hasName**: Workflow display name

**Relationships**:
- `is_a_type_of` → `#V#workflow`
- `has_instance` → Specific workflow instances (e.g., `#V#chat_assistant_workflow`)

#### `#V#llm_action` (Type) — ENHANCED

**Existing concept, enhance with additional attributes**:

**New Attributes**:
- **hasPromptTemplate**: Reference to stored prompt concept ID (e.g., `#V#find_concepts_in_text_prompt`)
- **hasModelSelectionStrategy**: `{"fixed", "adaptive", "user_preference"}`
- **hasInputSchema** (JSON): Expected input structure
- **hasOutputSchema** (JSON): Expected LLM response structure
- **hasValidationRules** (JSON): Post-conditions to verify

**Example Enhancement**:
```json
{
  "action_id": "#V#extract_spans_action",
  "prompt_template_id": "#V#find_concepts_in_text_prompt",
  "model_selection": "user_preference",
  "input_schema": {"text": "string", "max_spans": "integer"},
  "output_schema": {"spans": "array[{text, start, end, type}]"},
  "validation_rules": [
    {"rule": "output.spans must be array"},
    {"rule": "each span must have text field"}
  ]
}
```

#### `#V#workflow_step` (Type)

**Description**: A single step within an LLM workflow

**Attributes** (JSON in hasDefinition):
```json
{
  "step_id": "invoke_llm_for_spans",
  "step_index": 2,
  "action_id": "#V#extract_spans_action",
  "condition": "$.input.text.length > 0",
  "retry_policy": "exponential_backoff",
  "max_retries": 3,
  "timeout_seconds": 30
}
```

**Relationships**:
- `is_an_instance_of` → `#V#workflow_step`
- `part_of_workflow` → Parent workflow concept

#### `#V#workflow_execution_trace` (Individual)

**Description**: Runtime record of a specific workflow execution

**Attributes**:
- **workflow_id**: Reference to workflow definition
- **execution_id**: UUID for this specific run
- **start_time**: ISO 8601 timestamp
- **end_time**: ISO 8601 timestamp
- **status**: `{"running", "completed", "failed", "timeout", "cancelled"}`
- **user_id**: Authenticated user who triggered workflow
- **org_id**: Organisation context
- **step_traces**: Array of step-level execution records
- **total_tokens**: Aggregate token count across all LLM calls
- **total_cost**: Estimated cost (if available)

**Storage**: MongoDB `workflow_executions` collection (separate from main Vontology)

**Retention**: 30-day TTL for routine executions, indefinite for failed/timeout cases

### 5.2 Predicate Extensions

**New Predicates**:
- `partOfWorkflow`: Links step to parent workflow
- `hasPromptTemplate`: Links action to stored prompt concept
- `hasInputSchema` / `hasOutputSchema`: Schema definitions (JSON text relations)

---

## 6. Workflow Definition Language

### 6.1 JSON Schema

Workflows are stored as JSON in `hasDefinition` text relations:

```json
{
  "workflow_id": "#V#chat_assistant_workflow",
  "version": "1.0",
  "control_flow": "sequential",
  "failure_strategy": "halt",
  "timeout_seconds": 120,
  "steps": [
    {
      "step_id": "build_system_message",
      "action_type": "compose_prompt",
      "inputs": {
        "user_id": "$context.user_id",
        "org_id": "$context.org_id",
        "tool_list": "$system.available_tools"
      },
      "outputs": {
        "system_message": "$.result"
      }
    },
    {
      "step_id": "invoke_llm",
      "action_type": "llm_generate",
      "model": "$settings.default_model",
      "prompt": "$state.system_message + $inputs.user_prompt",
      "context": "$inputs.conversation_history",
      "max_retries": 2,
      "outputs": {
        "llm_response": "$.content"
      }
    },
    {
      "step_id": "extract_tool_calls",
      "action_type": "parse_json",
      "input": "$state.llm_response",
      "condition": "'action' in $state.llm_response",
      "fallback_action": "return_text_response",
      "outputs": {
        "tool_calls": "$.parsed.tools"
      }
    },
    {
      "step_id": "execute_tools",
      "action_type": "mcp_tool_loop",
      "tool_calls": "$state.tool_calls",
      "max_iterations": 8,
      "condition": "$state.tool_calls is not empty",
      "outputs": {
        "tool_results": "$.results"
      }
    },
    {
      "step_id": "synthesise_response",
      "action_type": "llm_generate",
      "prompt": "Synthesise final response from: $state.tool_results",
      "outputs": {
        "final_response": "$.content"
      }
    }
  ]
}
```

### 6.2 Variable Binding Syntax

- `$context.user_id`: Runtime context variable
- `$inputs.user_prompt`: Workflow input parameter
- `$state.llm_response`: Previous step output
- `$settings.default_model`: System settings
- `$system.available_tools`: System introspection

**Evaluation**: Simple JSONPath-like expressions (use `jsonpath-ng` library)

### 6.3 Condition Syntax

- String containment: `'word' in $state.response`
- Field presence: `$state.result.field exists`
- Comparison: `$state.count > 0`
- Boolean: `$state.flag == true`

**Parser**: Custom minimal expression evaluator (not full Python `eval` for security)

---

## 7. Control Flow Patterns

### 7.1 Sequential Workflow

**Definition**: Steps execute in order, each step's output becomes available to subsequent steps.

**Example** (Annotation Extraction):
```
1. load_prompt_from_vontology
2. build_extraction_prompt
3. invoke_llm_for_spans
4. parse_json_response (with fallback)
5. map_types_to_concepts
6. rank_candidates
```

**Implementation**: Simple `for` loop over steps list

### 7.2 Conditional Workflow

**Definition**: Steps execute based on runtime condition evaluation.

**Example** (Chat Intent Routing):
```
1. classify_user_intent
2. IF intent == "concept_query":
     3a. fetch_concept_details
     3b. synthesise_answer_with_context
   ELSE:
     3c. generate_direct_response
```

**Implementation**: Evaluate `condition` field before step execution

### 7.3 Iterative Workflow

**Definition**: Repeat a sub-workflow until condition met or max iterations reached.

**Example** (Rumination):
```
WHILE missing_fields AND iterations < max_iterations:
  1. identify_next_missing_field
  2. query_llm_for_value
  3. validate_response
  4. IF valid:
       update_knowledge_graph
```

**Implementation**: `control_flow: "iterative"` with `max_iterations` parameter

### 7.4 Parallel Workflow

**Definition**: Execute multiple steps concurrently, then aggregate results.

**Example** (Multi-Model Consensus):
```
PARALLEL:
  - invoke_llm_a(prompt)
  - invoke_llm_b(prompt)
  - invoke_llm_c(prompt)
AGGREGATE:
  - majority_vote([response_a, response_b, response_c])
```

**Implementation**: `asyncio.gather()` for concurrent step execution

### 7.5 DAG Workflow (Future)

**Definition**: Steps form a directed acyclic graph with explicit dependencies.

**Example** (Complex Analysis):
```
     ┌─→ step_2a ─┐
step_1 ─┤           ├─→ step_4
     └─→ step_2b ─┘
```

**Implementation**: Topological sort + parallel execution where possible

---

## 8. Implementation Architecture

### 8.1 Module Structure

```
src/backend/workflows/
├── __init__.py
├── engine.py                  # Core WorkflowEngine class
├── executors/
│   ├── __init__.py
│   ├── llm_executor.py        # LLM action execution
│   ├── mcp_tool_executor.py   # MCP tool invocation
│   ├── prompt_executor.py     # Prompt composition
│   └── parse_executor.py      # JSON/text parsing
├── definition.py              # Workflow definition parsing
├── state.py                   # WorkflowState and StateManager
├── trace.py                   # ExecutionTrace recording
└── validators.py              # Schema and condition validators
```

### 8.2 Core Classes

#### `LLMWorkflowEngine`

```python
from typing import Dict, Any, Optional
from .definition import WorkflowDefinition
from .state import WorkflowState
from .trace import ExecutionTrace

class LLMWorkflowEngine:
    def __init__(self, gateway: InternalMCPGateway):
        self._gateway = gateway
        self._executor_registry: Dict[str, Callable] = {}
        self._register_default_executors()

    def execute_workflow(
        self,
        workflow_id: str,
        inputs: Dict[str, Any],
        context: WorkflowContext,
    ) -> WorkflowResult:
        """Execute a workflow from its Vontology definition."""
        # 1. Load workflow definition
        workflow_def = self._load_workflow_definition(workflow_id)

        # 2. Initialise state
        state = WorkflowState(inputs=inputs, context=context)
        trace = ExecutionTrace(workflow_id=workflow_id, user_id=context.user_id)

        # 3. Execute according to control flow
        if workflow_def.control_flow == "sequential":
            result = self._execute_sequential(workflow_def, state, trace)
        elif workflow_def.control_flow == "conditional":
            result = self._execute_conditional(workflow_def, state, trace)
        elif workflow_def.control_flow == "iterative":
            result = self._execute_iterative(workflow_def, state, trace)
        else:
            raise ValueError(f"Unsupported control flow: {workflow_def.control_flow}")

        # 4. Persist trace
        self._save_trace(trace)

        return result

    def execute_step(
        self,
        step: WorkflowStep,
        state: WorkflowState,
        trace: ExecutionTrace,
    ) -> StepResult:
        """Execute a single workflow step."""
        # 1. Evaluate condition
        if step.condition and not self._evaluate_condition(step.condition, state):
            return StepResult(skipped=True)

        # 2. Resolve inputs
        resolved_inputs = self._resolve_inputs(step.inputs, state)

        # 3. Dispatch to executor
        executor = self._executor_registry.get(step.action_type)
        if not executor:
            raise ValueError(f"Unknown action type: {step.action_type}")

        # 4. Execute with retry logic
        result = self._execute_with_retry(
            executor, resolved_inputs, step.max_retries, step.retry_policy
        )

        # 5. Update state and trace
        state.update(step.outputs, result)
        trace.add_step(step.step_id, result)

        return result
```

#### `WorkflowDefinition`

```python
from dataclasses import dataclass
from typing import List, Dict, Any

@dataclass
class WorkflowStep:
    step_id: str
    action_type: str
    inputs: Dict[str, str]  # Variable bindings
    outputs: Dict[str, str]  # Output mappings
    condition: Optional[str] = None
    retry_policy: str = "none"
    max_retries: int = 0
    timeout_seconds: int = 30

@dataclass
class WorkflowDefinition:
    workflow_id: str
    version: str
    control_flow: str  # "sequential" | "conditional" | "iterative"
    steps: List[WorkflowStep]
    failure_strategy: str = "halt"
    max_iterations: Optional[int] = None
    timeout_seconds: int = 300

    @classmethod
    def from_vontology(cls, workflow_id: str) -> 'WorkflowDefinition':
        """Load workflow definition from Vontology."""
        # Fetch hasDefinition text relation
        # Parse JSON
        # Validate schema
        pass
```

#### `WorkflowState`

```python
class WorkflowState:
    """Manages workflow execution state (inputs, step outputs, context)."""

    def __init__(self, inputs: Dict[str, Any], context: WorkflowContext):
        self._inputs = inputs
        self._context = context
        self._outputs = {}  # step_id -> output
        self._variables = {}  # state variables

    def get(self, path: str) -> Any:
        """Resolve variable path (e.g., '$inputs.user_prompt')."""
        # Parse path: $namespace.key.subkey
        # Return value from appropriate namespace
        pass

    def update(self, mappings: Dict[str, str], result: Any):
        """Update state with step outputs."""
        # Apply output mappings (e.g., {"llm_response": "$.content"})
        # Extract values from result using JSONPath
        pass
```

#### `ExecutionTrace`

```python
@dataclass
class StepTrace:
    step_id: str
    start_time: datetime
    end_time: datetime
    status: str  # "success" | "failed" | "skipped"
    inputs: Dict[str, Any]
    outputs: Dict[str, Any]
    error: Optional[str] = None
    tokens_used: Optional[int] = None

class ExecutionTrace:
    """Records workflow execution for observability."""

    def __init__(self, workflow_id: str, user_id: str):
        self.workflow_id = workflow_id
        self.execution_id = str(uuid.uuid4())
        self.user_id = user_id
        self.start_time = datetime.utcnow()
        self.end_time: Optional[datetime] = None
        self.status = "running"
        self.steps: List[StepTrace] = []

    def add_step(self, step_id: str, result: StepResult):
        """Record step execution."""
        pass

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for storage."""
        pass
```

### 8.3 Executor Registry

**Executor Interface**:
```python
class StepExecutor(ABC):
    @abstractmethod
    def execute(self, inputs: Dict[str, Any], context: WorkflowContext) -> Any:
        """Execute the action and return result."""
        pass
```

**Built-in Executors**:

1. **LLMExecutor**: Invoke LLM with prompt/model/params
2. **MCPToolExecutor**: Call internal MCP tools
3. **PromptComposerExecutor**: Build prompts from templates
4. **JSONParserExecutor**: Parse JSON with fallback handling
5. **ValidatorExecutor**: Check post-conditions/schemas

**Registration**:
```python
def _register_default_executors(self):
    self._executor_registry["llm_generate"] = LLMExecutor(self._gateway)
    self._executor_registry["mcp_tool_invocation"] = MCPToolExecutor(self._gateway)
    self._executor_registry["compose_prompt"] = PromptComposerExecutor()
    self._executor_registry["parse_json"] = JSONParserExecutor()
    self._executor_registry["validate"] = ValidatorExecutor()
```

---

## 9. Migration Strategy

### 9.1 Phased Approach

**Phase 1: Foundation** (No breaking changes)
- Implement `LLMWorkflowEngine` with sequential flow only
- Create Vontology schema for workflows
- Add workflow storage/retrieval endpoints
- Introduce `WorkflowAdapter` to wrap existing functions

**Phase 2: Chat Integration** (Gradual replacement)
- Define `#V#chat_assistant_workflow` in Vontology
- Refactor `InternalMCPChatOrchestrator` to use workflow engine internally
- Preserve existing public API (`orchestrator.run()` signature unchanged)
- Run parallel validation (old vs new implementation)

**Phase 3: Annotation & Prompts** (Feature parity + enhancements)
- Convert annotation extraction to workflow
- Enable stored prompts to declare multi-step workflows
- Add conditional and iterative control flow

**Phase 4: New Features** (Leverage workflow capabilities)
- Parallel execution
- Workflow optimisation (caching, batching)
- UI for workflow visualisation
- Runtime workflow customisation

### 9.2 Adapter Pattern (Non-Breaking Wrapper)

```python
class WorkflowAdapter:
    """Wraps existing function as a single-step workflow."""

    @staticmethod
    def from_function(
        func: Callable,
        workflow_id: str,
        input_schema: Dict = None,
        output_schema: Dict = None,
    ) -> LLMWorkflow:
        """Convert existing function to workflow definition."""
        # Generate workflow JSON with single step
        # Step action = wrapped function call
        # Store in Vontology
        pass

# Example usage
annotation_workflow = WorkflowAdapter.from_function(
    extract_annotations,
    workflow_id="#V#annotation_extraction_workflow",
    input_schema={"text": "string"},
    output_schema={"spans": "array"},
)
```

### 9.3 Dual-Path Validation

During migration, run both old and new implementations:

```python
def extract_annotations_with_validation(text: str) -> List[Dict]:
    # Old implementation
    old_result = _extract_annotations_legacy(text)

    # New workflow implementation
    workflow_result = workflow_engine.execute_workflow(
        "#V#annotation_extraction_workflow",
        inputs={"text": text},
        context=get_current_context(),
    )
    new_result = workflow_result.outputs["spans"]

    # Compare results
    if not _results_match(old_result, new_result):
        logger.warning(f"Workflow divergence detected: {diff}")
        # Emit metric for monitoring

    # Return old result (safe fallback during transition)
    return old_result
```

---

## 10. Integration Points

### 10.1 Chat Assistant

**Before** (`orchestrator.py`):
```python
class InternalMCPChatOrchestrator:
    def run(self, prompt, context, ...) -> OrchestratorResult:
        # 400 lines of procedural orchestration logic
        pass
```

**After** (workflow-based):
```python
class InternalMCPChatOrchestrator:
    def __init__(self, gateway, workflow_engine=None):
        self._gateway = gateway
        self._workflow_engine = workflow_engine or LLMWorkflowEngine(gateway)
        self._chat_workflow_id = "#V#chat_assistant_workflow"

    def run(self, prompt, context, ...) -> OrchestratorResult:
        workflow_result = self._workflow_engine.execute_workflow(
            self._chat_workflow_id,
            inputs={"prompt": prompt, "context": context},
            context=WorkflowContext(
                user_id=get_authenticated_user_id(),
                org_id=get_current_organisation_id(),
            ),
        )

        # Map workflow result to OrchestratorResult
        return OrchestratorResult(
            response_text=workflow_result.outputs["final_response"],
            tool_invocations=workflow_result.trace.tool_calls,
            ...
        )
```

### 10.2 Annotation Extraction

**Before**:
```python
def extract_annotations(text: str) -> List[Dict]:
    prompt = load_prompt_from_vontology()
    llm_response = llm_client.generate(prompt + text)
    spans = parse_json_or_fallback(llm_response)
    return rank_candidates(spans)
```

**After**:
```python
def extract_annotations(text: str) -> List[Dict]:
    result = workflow_engine.execute_workflow(
        "#V#annotation_extraction_workflow",
        inputs={"text": text},
        context=get_current_context(),
    )
    return result.outputs["ranked_spans"]
```

### 10.3 Stored Prompts

**Enhancement**: Prompts can now declare workflows instead of just templates.

**Vontology Representation**:
```json
{
  "concept_id": "#V#my_custom_prompt",
  "hasDescription": "Analyse research paper and extract key findings",
  "hasWorkflowDefinition": {
    "control_flow": "sequential",
    "steps": [
      {"action_type": "llm_generate", "prompt": "Summarise abstract"},
      {"action_type": "llm_generate", "prompt": "Extract methodology"},
      {"action_type": "llm_generate", "prompt": "List key findings"},
      {"action_type": "validate", "rule": "findings.length >= 3"}
    ]
  }
}
```

---

## 11. Performance Considerations

### 11.1 Overhead Analysis

**Added Latency Sources**:
1. Workflow definition loading from Vontology (~10-20ms, cacheable)
2. State management and variable resolution (~5-10ms per step)
3. Trace recording (~2-5ms per step)
4. Condition evaluation (~1-2ms per step)

**Total Overhead**: ~20-40ms for typical workflow (5 steps)

**Target**: Keep overhead <5% of total workflow time (dominated by LLM calls which are 500ms-5s)

### 11.2 Optimisation Strategies

#### Caching

1. **Workflow Definition Cache**: TTL=300s (same as current prompt cache)
2. **Step Result Cache**: Cache idempotent step outputs (e.g., prompt composition)
3. **LLM Response Cache**: Deduplicate identical prompts within workflow

```python
class WorkflowCache:
    def __init__(self, ttl_seconds: int = 300):
        self._definitions: Dict[str, WorkflowDefinition] = {}
        self._step_results: Dict[str, Any] = {}
        self._llm_responses: Dict[str, str] = {}

    def get_cached_step_result(self, cache_key: str) -> Optional[Any]:
        # Check if step result can be reused
        pass
```

#### Parallel Execution

For workflows with independent steps, execute concurrently:

```python
async def _execute_parallel(
    self,
    workflow_def: WorkflowDefinition,
    state: WorkflowState,
    trace: ExecutionTrace,
) -> WorkflowResult:
    # Identify independent steps (no cross-dependencies)
    # Execute concurrently using asyncio.gather()
    pass
```

#### Lazy Loading

Load workflow definitions only when needed (not on server startup).

### 11.3 Performance Benchmarks

**Baseline** (current direct LLM calls):
- Annotation extraction: ~800ms (LLM call dominates)
- Chat response: ~1.2s (single turn, no tools)
- Chat with tools: ~3.5s (2 tool invocations)

**Target** (workflow-based):
- Annotation extraction: <850ms (+50ms overhead acceptable)
- Chat response: <1.25s (+50ms overhead)
- Chat with tools: <3.6s (+100ms overhead for more complex workflows)

---

## 12. Security & Access Control

### 12.1 Workflow-Level Permissions

**Principle**: Workflows inherit access control from authenticated user context.

**Implementation**:
```python
class WorkflowContext:
    user_id: str  # Authenticated user (#V#person_id)
    org_id: str   # Current organisation (#V#org_id)
    permissions: Set[str]  # User's role-based permissions

    def can_execute_workflow(self, workflow_id: str) -> bool:
        # Check if user has permission to run workflow
        # E.g., some workflows may be admin-only
        pass
```

### 12.2 Step-Level Access Control

**Scenario**: Workflow contains MCP tool calls that require specific permissions.

**Solution**: Workflow engine delegates permission checks to underlying systems (e.g., MCP gateway already enforces tool permissions).

### 12.3 Trace Sanitisation

**Concern**: Execution traces may contain sensitive data (user inputs, LLM responses).

**Mitigation**:
1. **Field-Level Redaction**: Automatically redact sensitive fields (e.g., API keys, passwords)
2. **User Consent**: Require opt-in for detailed trace collection
3. **Retention Policy**: Auto-delete traces after 30 days (configurable)

```python
class TraceSanitiser:
    SENSITIVE_PATTERNS = [
        r"api[_-]?key",
        r"password",
        r"token",
    ]

    def sanitise_trace(self, trace: ExecutionTrace) -> ExecutionTrace:
        # Redact sensitive fields from step inputs/outputs
        pass
```

### 12.4 Workflow Integrity

**Threat**: Malicious workflow definition could execute arbitrary code.

**Defences**:
1. **No Dynamic Code Execution**: Workflow definitions are data (JSON), not code
2. **Restricted Executor Registry**: Only pre-approved executors can be invoked
3. **Condition Evaluation Sandbox**: Use safe expression evaluator (not Python `eval`)
4. **Schema Validation**: All workflow definitions validated against schema before execution

---

## 13. Testing Strategy

### 13.1 Unit Tests

**Workflow Engine**:
- Workflow definition parsing
- Variable resolution
- Condition evaluation
- Retry logic
- Error handling

**Executors**:
- LLM executor (mocked LLM calls)
- MCP tool executor (mocked gateway)
- Prompt composer
- JSON parser (including fallback)

**State Management**:
- Variable binding
- Output mapping
- State updates

### 13.2 Integration Tests

**End-to-End Workflows**:
- Chat assistant workflow (full conversation)
- Annotation extraction workflow
- Multi-step stored prompt execution

**Cross-Component**:
- Workflow engine + MCP gateway
- Workflow engine + Vontology (load/store definitions)

### 13.3 Performance Tests

**Benchmarks**:
- Workflow overhead vs direct calls
- Parallel execution speedup
- Cache hit rates
- Trace storage impact

**Load Testing**:
- Concurrent workflow executions
- Maximum workflow steps before degradation
- Memory usage under load

### 13.4 Security Tests

**Access Control**:
- Unauthorised workflow execution blocked
- Step-level permission enforcement
- Trace access restricted to workflow owner

**Injection Attacks**:
- Malicious condition expressions rejected
- Invalid workflow definitions rejected
- Trace sanitisation verified

---

## 14. Operational Concerns

### 14.1 Monitoring & Observability

**Metrics** (exposed via `/diag` endpoint):
- Workflow execution count (by workflow_id)
- Average execution time (by workflow_id)
- Failure rate (by workflow_id, by step_id)
- Token usage (aggregate across all workflows)
- Cache hit rates (definition cache, step result cache)

**Dashboards**:
- Real-time workflow execution view
- Historical performance trends
- Error rate alerts

**Logging**:
```
[workflow_engine] Executing workflow #V#chat_assistant_workflow (execution_id=abc-123)
[workflow_engine] Step build_system_message completed (15ms)
[workflow_engine] Step invoke_llm completed (1203ms, tokens=450)
[workflow_engine] Workflow completed successfully (total=1.5s)
```

### 14.2 Error Handling & Debugging

**Workflow Failure Modes**:
1. **Step Timeout**: Individual step exceeds timeout
2. **Step Error**: Executor raises exception
3. **Validation Failure**: Output doesn't match schema
4. **Workflow Timeout**: Overall workflow exceeds timeout

**Recovery Strategies** (configured via `failure_strategy`):
- **halt**: Stop workflow immediately, return partial results
- **continue**: Skip failed step, proceed to next
- **retry**: Retry failed step (up to max_retries)
- **fallback**: Execute alternate step (if defined)

**Debugging Tools**:
- Execution trace viewer (UI showing step-by-step execution)
- Step replay (re-execute single step with same inputs)
- Workflow diff (compare execution traces to identify regressions)

### 14.3 Deployment & Rollback

**Feature Flags**:
- `VON_WORKFLOWS_ENABLED`: Global enable/disable
- `VON_WORKFLOW_<ID>_ENABLED`: Per-workflow feature flag

**Rollback Plan**:
1. Disable workflow execution via feature flag
2. Fall back to legacy implementation (maintained in parallel during Phase 2)
3. Investigate failure, fix workflow definition
4. Re-enable once validated

---

## 15. Future Extensions

### 15.1 Workflow Marketplace

**Vision**: Users can share and discover reusable workflows.

**Features**:
- Workflow templates for common tasks
- Community ratings and reviews
- Import/export workflows between instances

### 15.2 Visual Workflow Builder

**Vision**: Drag-and-drop UI for workflow creation.

**Features**:
- Node-based workflow editor (similar to n8n, Flowise)
- Real-time validation
- Step previews (execute single step to test)

### 15.3 Adaptive Workflows

**Vision**: Workflows that learn from execution history to optimise themselves.

**Examples**:
- Adjust retry parameters based on success rate
- Skip unnecessary steps based on input patterns
- Switch models based on performance/cost trade-offs

### 15.4 Cross-System Workflows

**Vision**: Workflows that orchestrate external systems (not just LLMs).

**Examples**:
- Workflow that queries database → invokes LLM → updates CRM
- Integration with Zapier, IFTTT for external automation

---

## 16. Glossary

**LLM Action**: Atomic unit — a single LLM invocation with specific prompt/model/params.

**LLM Workflow**: Composition of actions (or sub-workflows) with control flow logic.

**Workflow Step**: Single action within a workflow, potentially with condition and retry logic.

**Workflow Definition**: Declarative specification of workflow structure (stored in Vontology).

**Workflow Execution**: Runtime instance of a workflow being executed.

**Execution Trace**: Complete record of a workflow execution (inputs, outputs, timing, errors).

**Workflow Context**: User/org/permissions associated with workflow execution.

**Workflow State**: Runtime variables and step outputs during execution.

**Executor**: Component that executes a specific action type (e.g., LLM call, tool invocation).

**Control Flow**: Logic determining which steps execute and in what order (sequential, conditional, iterative, etc.).

**Failure Strategy**: How workflow handles step failures (halt, continue, retry, fallback).

---

## Revision History

| Version | Date       | Changes                          | Author           |
|---------|------------|----------------------------------|------------------|
| 1.0     | 2025-12-21 | Initial design document          | Von AI Assistant |

---

**End of Document**
