"""Documentation of current tool-call patterns and audit findings.

This document captures the current state of tool invocation across Von,
providing the baseline for Phase 0 (JVNAUTOSCI-799).
"""

# PHASE 0 AUDIT: Current Tool-Call Patterns

## 1. Current Invocation Paths

### 1.1 Internal MCP Orchestrator (`src/backend/integrations/internal_mcp/orchestrator.py`)

**JSON-in-Text Parsing**:
- Method: `_extract_json_blob()` (line 889-969)
- Pattern: Model outputs tool calls as JSON object embedded in text
- Format: `{"action": "call_tool", "tool": "...", "payload": {...}}`
- Validation: Tolerates Markdown fences and trailing prose

**Safety Constraints** (must preserve in Phase 3):
1. Single-tool-call contract: Only one tool call per response
2. Namespace injection rules: Tool names must match registered tools
3. Untrusted content: No tool execution from user-pasted JSON
4. Strict JSON parsing: Malformed JSON raises `ToolCallParsingError`

**Failure Modes** (documented in JVNAUTOSCI-698, JVNAUTOSCI-799):
1. Model hallucination: "I cannot access tools" despite server truth showing available tools
2. Parsing ambiguity: Trailing `}x` or multiple JSON objects cause failures
3. Non-determinism: Different runs of same prompt may parse differently

### 1.2 Prompt-Introspection Fast-Path (`/von/generate` endpoint)

**Location**: `src/backend/server/chat_routes.py` or `routes.py`

**Purpose**: Deterministically answer runtime state questions without parsing ambiguity
- Returns: Prompt IDs, text, model configuration
- Avoids: Model self-reporting runtime state

**Temporary Mitigation**: Fast-path should be removed post-JVNAUTOSCI-799 (Phase 5)

### 1.3 Annotation Extraction Service

**Location**: `src/backend/services/annotation_extraction_service.py`

**Current Pattern**:
1. Load prompt from Vontology (`#V#find_concepts_in_text_prompt`)
2. Invoke LLM
3. Parse JSON response with fallback to regex extraction
4. Map type labels → concept IDs

**Tool Interaction**: Doesn't currently invoke tools, but could benefit from structured LLM responses

### 1.4 Tell-Von Agent (Task Inference)

**Status**: Under development (JVNAUTOSCI-552)

**Anticipated Tool Usage**:
- Natural language → structured task specification
- May require tool calling for intent clarification

## 2. Current Contract & Semantics

### 2.1 Single-Tool-Call Semantics

**Current Assumption**:
- Each LLM response contains at most one tool call
- Multiple tools require sequential rounds

**Implication for Workflows** (JVNAUTOSCI-803):
- Conditional workflows: IF tool X succeeds THEN tool Y
- Sequential workflows: tool A → tool B → tool C
- Parallel workflows: NOT SUPPORTED YET (future feature)

### 2.2 Tool Namespace Isolation

**Current Pattern**:
- Internal MCP tools (gateway provides ~50+ tools)
- User-provided tools (via stored prompts): FUTURE
- External tools (Zapier, etc.): FUTURE (Phase 4+)

**Safety Model**:
- Whitelist of allowed tools per user/org/context
- No direct access to arbitrary executables
- Tool execution logged and auditable

### 2.3 Retry Semantics

**Current Behaviour**:
- No automatic retry at tool call level
- Orchestrator implements max_tool_invocations loop (default 1, max 8)
- Model can decide to call another tool or respond with text

**For Workflows** (JVNAUTOSCI-803):
- Retry logic should be at step level (workflow step executor)
- Exponential backoff, max retries, timeout safeguards

## 3. Observed Failure Examples

### 3.1 Runtime State Hallucination (JVNAUTOSCI-799)

**Symptom**: Model claims "no user-specific prompt active"
**Server Truth**: `user_prompt.loaded=true`, concept IDs present
**Root Cause**: Model trained on limited context; cannot introspect runtime state

**Mitigation** (Temporary - Phase 5 removes fast-path):
```
/von/generate?query=is_prompt_active
Response: {"active": true, "prompt_id": "#V#my_prompt", "text": "..."}
```

### 3.2 JSON Parsing Ambiguity (JVNAUTOSCI-698)

**Symptom**: Tool call with trailing `}x` fails parsing
**Current Handling**: Strict JSONDecodeError raises `ToolCallParsingError`
**Root Cause**: Model sometimes appends explanatory text without proper separation

**Mitigation** (JVNAUTOSCI-799 Phase 1-2):
Structured tool calling replaces this with provider-native API

### 3.3 Multiple JSON Values in Response

**Symptom**: Model outputs multiple tool calls or multiple JSON objects
**Current Handling**: Only first valid JSON object accepted (strict mode)
**Future Handling**: Structured calling returns List[ToolCall]

## 4. Provider-Specific Considerations

### 4.1 OpenAI (GPT-4, GPT-3.5 Turbo)

**Native Support**: Function calling API (via `tools` parameter)
**Advantages**:
- Deterministic structured output
- Reliable tool invocation
- Token usage tracking

**Adoption Strategy** (Phase 2):
1. Implement `OpenAIClient.generate_with_tools()`
2. Map internal `ToolDefinition` → OpenAI `tools` schema
3. Parse `message.tool_calls` → canonical `ToolCall` list
4. Enable via feature flag `VON_INTERNAL_MCP_STRUCTURED_TOOL_CALLING=1`

### 4.2 Gemini (Gemini Pro, Pro Vision)

**Native Support**: Function calling API (via `tools` parameter)
**Implementation**: Similar to OpenAI but with Gemini-specific schema mapping

### 4.3 Ollama (Local Models)

**Native Support**: None (GGML models don't have function calling)
**Fallback Strategy**:
1. Grammar-based constrained generation (if GGML grammar available)
2. JSON-in-text parsing with strict validation (default)
3. Prompt engineering to elicit proper JSON format

**Migration Path**: Ollama users can upgrade to models with better JSON support

## 5. Integration Points with JVNAUTOSCI-803 (LLM Workflows)

### 5.1 Workflow Step Executor Requirements

**What Workflows Need** (from JVNAUTOSCI-803):
1. Structured tool calls (not text parsing)
2. Deterministic call_id for execution tracing
3. ToolDefinition schema compatible with workflow step definitions
4. Async/sync support for concurrent steps (parallel workflows)

**What JVNAUTOSCI-799 Provides**:
1. `LLMClient` abstract interface with `generate_with_tools()`
2. `ToolCall` with `call_id` for tracing
3. `ToolDefinition` with `output_schema` for validation
4. Both async and sync methods

### 5.2 Execution Trace Recording

**Workflow Execution Traces Need**:
- `ToolCall.call_id` to correlate with step results
- Deterministic tool invocation (no parsing ambiguity)
- Clear success/failure semantics (tool executed vs tool call invalid)

**JVNAUTOSCI-799 Enables This**:
- Structured `LLMResponse.tool_calls` (not text extraction)
- Preserves call_id through entire execution pipeline
- Provider-native APIs guarantee determinism

## 6. Feature Flag Strategy

### 6.1 Rollout Plan

**Default**: `VON_INTERNAL_MCP_STRUCTURED_TOOL_CALLING=1` (enabled)

**Disable for Debugging**: Set to `0` to revert to JSON-in-text parsing

**Legacy Fallback**: `VON_LEGACY_JSON_TEXT_PARSING=1` (disabled by default)

### 6.2 Transition Timeline

- Phase 1-2 (Weeks 1-2): Implement `OpenAIClient` with feature flag enabled
- Phase 2 (Weeks 2-3): Add Gemini/Ollama adapters
- Phase 3 (Weeks 3-4): Integrate with orchestrator
- Phase 4-5 (Weeks 5+): Evaluate and deprecate legacy paths

## 7. Test Coverage Strategy

### 7.1 Regression Tests

Required tests to ensure JVNAUTOSCI-799 fixes original issues:

1. **Runtime State Hallucination**:
   - Test: "is_prompt_active" question must use tool-backed evidence
   - Verify: Model cannot claim "prompt not active" when it is

2. **JSON Parsing Reliability**:
   - Test: Malformed JSON with trailing `}x` does not execute tool
   - Verify: `ToolCallError` raised, not silent failure

3. **Tool Name Validation**:
   - Test: Requesting unknown tool does not execute
   - Verify: Error logged, tool call skipped

### 7.2 Acceptance Tests (from JVNAUTOSCI-799)

- Orchestrator uses structured calls (not `_extract_json_blob`)
- Prompt/model/RAG introspection returns tool-backed evidence
- Safety constraints preserved (whitelist, namespace rules)
- Tests cover observed failure modes

### 7.3 Integration Tests (for JVNAUTOSCI-803)

- Workflow step executes LLM action with tool calling
- Tool call `call_id` roundtrips through execution trace
- Multi-step workflows with tool dependencies work reliably

## 8. Backward Compatibility

### 8.1 Adapter Pattern

Existing code continues using `llm_interface.py`:

```python
# OLD API (still works)
from src.backend.languagemodels.llm_interface import get_llm_client
client = get_llm_client()
response = client.generate(prompt, context)

# NEW API (structured tool calling)
from src.backend.languagemodels.structured_tool_calling import get_llm_client, LLMClientConfig
config = LLMClientConfig(model="gpt-4")
client = get_llm_client(config)
response = client.generate_with_tools_sync(prompt, tools, context)
```

### 8.2 Gradual Migration

1. Orchestrator: Switch to `LLMClient.generate_with_tools()` (Phase 3)
2. Annotation: Optional migration to structured responses (Phase 3)
3. New workflows: Use structured calling by default (JVNAUTOSCI-803)
4. Legacy paths: Remove post-Phase 5 (deprecation notice added to `llm_interface.py`)

## 9. Known Limitations & Future Work

### 9.1 Current Limitations

1. **Ollama**: No native function calling → relies on prompt engineering + JSON parsing
2. **Multiple Tools**: Single-call contract → parallel workflows require sequence
3. **Tool Composition**: No tool chaining (tool A output → tool B input)

### 9.2 Future Enhancements (Phase 4+)

1. **Parallel Tool Execution**: Multiple tools in single LLM response
2. **Tool Chaining**: Automatic dataflow between tool calls
3. **Conditional Execution**: Tools selected based on input classification
4. **Adaptive Retry**: Retry strategy learns from success/failure patterns

## Conclusion

This audit confirms that JVNAUTOSCI-799's structured tool calling directly addresses
three categories of current problems:

1. **Reliability**: Deterministic tool invocation replaces ambiguous JSON parsing
2. **Observability**: Structured `ToolCall` objects enable accurate execution traces
3. **Workflow Foundation**: Canonical interfaces enable JVNAUTOSCI-803 (LLM Workflows)

The implementation prioritises OpenAI first (highest reliability requirement), then
adds Gemini and Ollama support for breadth of coverage.
"""

# Summary Table: Current Patterns vs Proposed

"""
Current State (JSON-in-Text):
┌─────────────────────┬─────────────────────────────┐
│ Aspect              │ Current Implementation      │
├─────────────────────┼─────────────────────────────┤
│ Tool Invocation     │ JSON extracted from text    │
│ Parser              │ Custom _extract_json_blob() │
│ Reliability         │ Prone to hallucination      │
│ Tracing             │ No call_id or structured    │
│ Multi-Tool Support  │ Single tool per response    │
│ Provider Support    │ All (via text parsing)      │
│ Async Support       │ None                        │
│ Retry Logic         │ In orchestrator loop        │
└─────────────────────┴─────────────────────────────┘

Proposed State (JVNAUTOSCI-799):
┌─────────────────────┬─────────────────────────────┐
│ Aspect              │ Structured Calling          │
├─────────────────────┼─────────────────────────────┤
│ Tool Invocation     │ Provider-native API         │
│ Parser              │ Provider SDK (no custom)    │
│ Reliability         │ Deterministic               │
│ Tracing             │ call_id on ToolCall         │
│ Multi-Tool Support  │ Future (Phase 4)            │
│ Provider Support    │ OpenAI, Gemini, Ollama      │
│ Async Support       │ async/sync both available   │
│ Retry Logic         │ At adapter layer            │
└─────────────────────┴─────────────────────────────┘
"""
