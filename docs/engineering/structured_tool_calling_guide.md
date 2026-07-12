# Structured Tool Calling Module — Historical Usage Guide

> **Document status: Legacy component reference; last evidenced during
> JVNAUTOSCI-799 in 2025.** The component still exists, but API names, provider
> examples, fallbacks, and integration behaviour below are not guaranteed
> current. Inspect the current package and targeted tests before copying code.

## Overview

The structured tool calling module (`src/backend/languagemodels/structured_tool_calling/`)
provides unified, provider-agnostic support for LLM tool invocation using structured
outputs instead of JSON-in-text parsing.

**Key Benefits**:
- Deterministic tool invocation (no parsing ambiguity)
- Provider-native function calling (OpenAI, Gemini)
- Graceful fallback for local models (Ollama)
- Execution tracing support (for JVNAUTOSCI-803 workflows)
- Async/sync API for concurrent operations

**JIRA Context**: JVNAUTOSCI-799 (Structured tool calling for internal MCP)

## Quick Start

### Basic Usage

```python
from src.backend.languagemodels.structured_tool_calling import (
    LLMClientConfig,
    ToolDefinition,
    get_llm_client,
)

# Create configuration
config = LLMClientConfig(
    model="gpt-4",
    api_key="sk-...",
    temperature=0.7,
)

# Instantiate client (factory selects right provider)
client = get_llm_client(config)

# Define tools
tools = [
    ToolDefinition(
        name="get_concept",
        description="Retrieve concept details from Vontology",
        input_schema={
            "type": "object",
            "properties": {
                "concept_id": {"type": "string", "description": "Concept ID (e.g., #V#person)"},
            },
            "required": ["concept_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
            },
        },
    ),
]

# Generate with tool calling
response = client.generate_with_tools_sync(
    prompt="What is a person?",
    available_tools=tools,
    system_message="You are a helpful assistant with access to a knowledge base.",
)

# Check result
if response.has_tool_calls():
    for tool_call in response.tool_calls:
        print(f"Tool: {tool_call.tool_name}")
        print(f"Arguments: {tool_call.payload}")
        print(f"Trace ID: {tool_call.call_id}")
else:
    print(f"Response: {response.text_response}")
```

### Async Usage

```python
import asyncio

async def main():
    config = LLMClientConfig(model="gpt-4")
    client = get_llm_client(config)

    response = await client.generate_with_tools(
        prompt="What is a person?",
        available_tools=tools,
    )

    return response

result = asyncio.run(main())
```

## Architecture

### Core Types (`types.py`)

**`ToolDefinition`**: Describes a tool available to the LLM.
```python
@dataclass
class ToolDefinition:
    name: str                              # Tool identifier
    description: str                       # Human-readable description
    input_schema: Dict[str, Any]          # JSON Schema for inputs
    output_schema: Optional[Dict[str, Any]]  # JSON Schema for outputs (for validation)
```

**`ToolCall`**: Canonical representation of a tool invocation.
```python
@dataclass
class ToolCall:
    tool_name: str                # Name of tool to invoke
    payload: Dict[str, Any]       # Tool arguments
    call_id: str                  # Unique ID for execution tracing (UUID4)
    timestamp: datetime           # When call was created (UTC)
```

**`LLMResponse`**: Response from LLM (text, tool calls, or both).
```python
@dataclass
class LLMResponse:
    text_response: str                    # Natural language text
    tool_calls: List[ToolCall]            # Structured tool invocations
    raw_response: Optional[Dict[str, Any]]  # Provider-specific response (debug)
    model: Optional[str]                  # Model used
    usage: Optional[Dict[str, Any]]       # Token usage info
```

### Client Interface (`client.py`)

**`LLMClient`**: Abstract base class for all provider implementations.

Methods:
- `async generate_with_tools()`: Async tool invocation
- `generate_with_tools_sync()`: Synchronous wrapper
- `_tool_definition_to_dict()`: Convert to provider format (overrideable)
- `_validate_input_schema()`: Schema validation

All implementations must provide both async and sync methods.

### Provider Implementations (`providers/`)

**`OpenAIClient`**: OpenAI GPT-4, GPT-3.5 Turbo
- Uses native `tools` parameter (function calling)
- Highest reliability and determinism
- Full token usage tracking

**`GeminiClient`**: Google Gemini Pro/Vision
- Uses Gemini function calling API
- Maps `ToolDefinition` → Gemini schema
- Limited token usage tracking

**`OllamaClient`**: Local models (Llama 2, Mistral, etc.)
- Fallback to prompt engineering + JSON parsing
- Optional grammar-based constrained generation
- No native function calling support

### Factory (`factory.py`)

**`get_llm_client(config: LLMClientConfig) -> LLMClient`**

Auto-selects provider based on model name:
```python
client = get_llm_client(LLMClientConfig(model="gpt-4"))      # → OpenAIClient
client = get_llm_client(LLMClientConfig(model="gemini-pro"))  # → GeminiClient
client = get_llm_client(LLMClientConfig(model="llama2"))      # → OllamaClient
```

## Configuration

### `LLMClientConfig`

```python
@dataclass
class LLMClientConfig:
    model: str                           # Model identifier (required)
    api_key: Optional[str] = None        # Authentication token
    base_url: Optional[str] = None       # API endpoint (for proxies, self-hosted)
    temperature: float = 0.7             # Sampling temperature (0-2)
    max_tokens: Optional[int] = None     # Max output tokens
    enable_structured_calling: bool = True   # Use structured tool calling
    fallback_to_json_text: bool = True   # Fallback if structured fails
```

### Feature Flags

**`VON_INTERNAL_MCP_STRUCTURED_TOOL_CALLING`** (env var)
- Default: `1` (enabled)
- Set to `0` to disable structured calling (use legacy JSON-in-text)

**`VON_LEGACY_JSON_TEXT_PARSING`** (env var)
- Default: `0` (disabled)
- Set to `1` to force legacy parsing (debugging only)

## Integration with Orchestrator (Phase 3)

The structured tool calling interface will replace `InternalMCPChatOrchestrator`'s
`_extract_json_blob()` method:

```python
# BEFORE (current): JSON-in-text parsing
raw_response = llm_client.generate(prompt, context)
tool_call = orchestrator._extract_json_blob(raw_response)

# AFTER (JVNAUTOSCI-799): Structured tool calling
response = llm_client.generate_with_tools_sync(
    prompt,
    available_tools=available_tools,
    context=context,
)
for tool_call in response.tool_calls:
    orchestrator._execute_tool(tool_call)
```

## Testing

### Unit Tests

Run tests for types and client:
```bash
pdm run pytest tests/backend/test_structured_tool_calling_types.py -v
pdm run pytest tests/backend/test_structured_tool_calling_client.py -v
```

### Regression Tests (Phase 4)

Tests to validate JVNAUTOSCI-799 fixes:
1. Runtime state hallucination: Model cannot claim "tool unavailable" when it is
2. JSON parsing reliability: Malformed JSON doesn't execute tools
3. Tool name validation: Unknown tools are rejected

### Integration Tests (JVNAUTOSCI-803)

Tests for workflow step execution:
1. Workflow step invokes LLM with tool calling
2. Tool call `call_id` preserved in execution trace
3. Multi-step workflows work reliably

## Error Handling

### `ToolCallError`

Raised when tool calling fails:
```python
try:
    response = client.generate_with_tools_sync(prompt, tools)
except ToolCallError as exc:
    logger.error(f"Tool calling failed: {exc}")
    if client.config.fallback_to_json_text:
        # Fall back to legacy parsing
        pass
```

### Validation Errors

```python
# Invalid tool definition
ToolDefinition(name="", description="Test", input_schema={})
# → ValueError: Tool name must be a non-empty string

# Invalid schema
ToolDefinition(
    name="test",
    description="Test",
    input_schema={"description": "no type"},
)
# → ValueError: input_schema must have 'type' or 'properties'
```

## Best Practices

### 1. Define Clear Tool Schemas

Use detailed JSON Schema for inputs to guide model:

```python
ToolDefinition(
    name="search_concepts",
    description="Search Vontology for concepts matching criteria",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (e.g., 'person', 'organisation')",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results (default 10, max 100)",
                "default": 10,
            },
            "filter_type": {
                "type": "string",
                "description": "Filter by concept type",
                "enum": ["type", "instance", "all"],
            },
        },
        "required": ["query"],
    },
    output_schema={
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "type": {"type": "string"},
            },
        },
    },
)
```

### 2. Handle Tool Calls Safely

```python
for tool_call in response.tool_calls:
    try:
        # Validate tool exists
        if tool_call.tool_name not in available_tools_map:
            logger.warning(f"Unknown tool: {tool_call.tool_name}")
            continue

        # Execute tool
        result = execute_tool(tool_call)

        # Log for tracing
        logger.info(f"Tool executed: {tool_call.call_id} ({tool_call.tool_name})")

    except Exception as exc:
        logger.error(f"Tool execution failed: {exc}")
        continue
```

### 3. Use Call IDs for Tracing

The `call_id` enables correlation with execution traces:

```python
# In orchestrator
tool_call = response.tool_calls[0]
result = execute_tool(tool_call)

# In execution trace (JVNAUTOSCI-803)
trace.add_step(
    step_id="invoke_tool",
    tool_call_id=tool_call.call_id,  # ← Correlate with tool call
    result=result,
)
```

### 4. Batch Tool Definitions

Reuse common tool definitions:

```python
# common_tools.py
CONCEPT_TOOLS = [
    ToolDefinition(
        name="get_concept",
        description="...",
        input_schema={...},
    ),
    ToolDefinition(
        name="list_concepts",
        description="...",
        input_schema={...},
    ),
]

# In orchestrator
response = client.generate_with_tools_sync(
    prompt,
    available_tools=CONCEPT_TOOLS,
)
```

## Migration Guide (JVNAUTOSCI-799 Phases)

### Phase 1-2: Implement (In Progress)
- ✅ Core types and interfaces
- ✅ OpenAI provider
- ✅ Gemini provider
- ✅ Ollama provider
- ✅ Factory and configuration
- ⏳ Feature flag integration

### Phase 3: Integrate with Orchestrator
- Replace `_extract_json_blob()` with `generate_with_tools()`
- Preserve safety constraints (whitelist, namespace rules)
- Add execution trace correlation (call_id)

### Phase 4-5: Deprecate Legacy
- Disable JSON-in-text parsing by default
- Remove or mark `_extract_json_blob()` as deprecated
- Publish integration guide for workflows (JVNAUTOSCI-803)

## Troubleshooting

### Tool Not Invoked

**Problem**: Model doesn't call expected tool

**Checks**:
1. Is tool in `available_tools` list?
2. Does tool `name` match what model expects?
3. Is tool description clear and specific?
4. Does prompt explicitly request tool usage?

### JSON Parsing Failures (Fallback Path)

**Problem**: Fallback to JSON-in-text parsing fails

**Checks**:
1. Enable debug logging: `VON_DEBUG_LLM_IO=1`
2. Check raw model output format
3. Validate schema against output
4. For Ollama, check prompt engineering in `OllamaClient._build_constrained_prompt()`

### Token Limit Exceeded

**Problem**: Response cut off or empty

**Checks**:
1. Increase `max_tokens` in config
2. Reduce context length
3. Simplify tool schemas
4. Check model token limits

## References

- **JVNAUTOSCI-799**: Structured tool calling task
- **JVNAUTOSCI-803**: LLM Workflows (depends on this module)
- **JVNAUTOSCI-698**: JSON action output prevention (original issue)
- **Phase 0 Audit**: `docs/engineering/jvnautosci_799_phase0_audit.md`

## File Structure

```
src/backend/languagemodels/structured_tool_calling/
├── __init__.py              # Public API exports
├── types.py                 # ToolCall, ToolDefinition, LLMResponse
├── client.py                # LLMClient abstract interface
├── factory.py               # get_llm_client() factory function
└── providers/
    ├── __init__.py
    ├── openai_client.py     # OpenAI GPT-4, GPT-3.5
    ├── gemini_client.py     # Google Gemini
    └── ollama_client.py     # Local models

tests/backend/
├── test_structured_tool_calling_types.py
└── test_structured_tool_calling_client.py

docs/engineering/
└── jvnautosci_799_phase0_audit.md  # Current state audit
```

## Authors & Maintainers

- GitHub Copilot (AI implementation assistant)
- Michael Witbrock (Von Lab, University of Auckland)

See JIRA task JVNAUTOSCI-799 for detailed design and acceptance criteria.
