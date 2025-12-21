"""
JVNAUTOSCI-799: Structured Tool Calling - Quick Reference

Short reference for using the structured tool calling module.
For detailed docs, see: structured_tool_calling_guide.md
"""

## Import Everything You Need

```python
from src.backend.languagemodels.structured_tool_calling import (
    LLMClientConfig,
    ToolDefinition,
    ToolCall,
    LLMResponse,
    ToolCallError,
    get_llm_client,
    LLMClient,
)
```

## Create a Client (Factory Pattern)

```python
from src.backend.languagemodels.structured_tool_calling import LLMClientConfig, get_llm_client

# Auto-selects provider by model name
config = LLMClientConfig(
    model="gpt-4",          # or "gemini-pro", "llama2", etc.
    api_key="sk-...",       # Optional, uses env var if not provided
    temperature=0.7,        # Default: 0.7
)

client = get_llm_client(config)
```

## Define Tools

```python
tools = [
    ToolDefinition(
        name="search_concepts",
        description="Search Vontology for concepts",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
        output_schema={
            "type": "array",
            "items": {"type": "object"},
        },
    ),
    ToolDefinition(
        name="get_concept",
        description="Get concept details",
        input_schema={
            "type": "object",
            "properties": {
                "concept_id": {"type": "string"},
            },
            "required": ["concept_id"],
        },
    ),
]
```

## Generate with Tools (Synchronous)

```python
response = client.generate_with_tools_sync(
    prompt="What is a person in the knowledge base?",
    available_tools=tools,
    system_message="You are a helpful assistant with access to a knowledge base.",
)

# Check response
print(f"Text: {response.text_response}")
print(f"Model: {response.model}")

if response.has_tool_calls():
    for tool_call in response.tool_calls:
        print(f"Tool: {tool_call.tool_name}")
        print(f"Args: {tool_call.payload}")
        print(f"Trace ID: {tool_call.call_id}")  # ← Use this for execution tracing
```

## Generate with Tools (Asynchronous)

```python
import asyncio

async def main():
    response = await client.generate_with_tools(
        prompt="What is a person in the knowledge base?",
        available_tools=tools,
    )
    return response

response = asyncio.run(main())
```

## Execute Tools

```python
def execute_tool(tool_call: ToolCall, tools_map: dict) -> dict:
    """Execute a tool call and return result"""

    if tool_call.tool_name not in tools_map:
        raise ValueError(f"Unknown tool: {tool_call.tool_name}")

    tool_function = tools_map[tool_call.tool_name]
    result = tool_function(**tool_call.payload)

    # Log for tracing (IMPORTANT: Use call_id)
    logger.info(f"Tool executed: {tool_call.call_id} ({tool_call.tool_name})")

    return result

# Usage
tools_map = {
    "search_concepts": search_concepts_fn,
    "get_concept": get_concept_fn,
}

for tool_call in response.tool_calls:
    result = execute_tool(tool_call, tools_map)
    print(f"Tool result: {result}")
```

## Handle Errors

```python
from src.backend.languagemodels.structured_tool_calling import ToolCallError

try:
    response = client.generate_with_tools_sync(
        prompt="...",
        available_tools=tools,
    )
except ToolCallError as e:
    logger.error(f"Tool calling failed: {e}")
    # Fallback to text-only response or retry
    response = client.generate_with_tools_sync(
        prompt="...",
        available_tools=[],  # Empty tools = text-only response
    )
```

## Provider Reference

### OpenAI (gpt-4, gpt-3.5-turbo)
- ✅ Native function calling
- ✅ Full token usage tracking
- ✅ Highest reliability
- Requires: OPENAI_API_KEY env var

### Gemini (gemini-pro, gemini-1.5-pro)
- ✅ Native function calling
- ✅ Schema mapping
- Limited token usage tracking
- Requires: GOOGLE_API_KEY env var

### Ollama (llama2, mistral, etc.)
- ✅ Works offline (local models)
- ✅ No API keys needed
- Uses constrained prompting + JSON extraction
- Requires: Ollama server running locally (default: http://localhost:11434)

## Feature Flags

```python
import os

# Enable/disable structured tool calling
os.environ["VON_INTERNAL_MCP_STRUCTURED_TOOL_CALLING"] = "1"  # Default: enabled

# Force legacy JSON-in-text parsing (debugging only)
os.environ["VON_LEGACY_JSON_TEXT_PARSING"] = "1"  # Default: disabled
```

## LLMResponse API

```python
response: LLMResponse
response.text_response          # str: Natural language output
response.tool_calls             # List[ToolCall]: Structured invocations
response.has_tool_calls()       # bool: Whether any tools were called
response.model                  # str: Model name (optional)
response.usage                  # Dict: Token usage (optional)
```

## ToolCall API

```python
tool_call: ToolCall
tool_call.tool_name             # str: Tool identifier
tool_call.payload               # Dict: Tool arguments
tool_call.call_id               # str: Unique ID for tracing (UUID4)
tool_call.timestamp             # datetime: When call was created (UTC)
```

## Common Patterns

### Pattern 1: Chat Loop with Tools

```python
def chat_with_tools(user_message: str, tools: List[ToolDefinition]) -> str:
    """Simple chat loop with tool calling"""

    response = client.generate_with_tools_sync(
        prompt=user_message,
        available_tools=tools,
    )

    if response.has_tool_calls():
        results = []
        for tool_call in response.tool_calls:
            result = execute_tool(tool_call)
            results.append(f"{tool_call.tool_name}: {result}")

        # Feed tool results back to model
        followup = client.generate_with_tools_sync(
            prompt=f"Tool results:\n" + "\n".join(results),
            available_tools=tools,
        )
        return followup.text_response
    else:
        return response.text_response
```

### Pattern 2: Tool Result Aggregation

```python
def aggregate_tool_results(response: LLMResponse, tools_map: dict) -> List[dict]:
    """Execute all tool calls and aggregate results"""

    results = []
    for tool_call in response.tool_calls:
        try:
            result = tools_map[tool_call.tool_name](**tool_call.payload)
            results.append({
                "call_id": tool_call.call_id,        # ← Keep call_id for tracing
                "tool_name": tool_call.tool_name,
                "result": result,
                "success": True,
            })
        except Exception as e:
            results.append({
                "call_id": tool_call.call_id,
                "tool_name": tool_call.tool_name,
                "error": str(e),
                "success": False,
            })

    return results
```

### Pattern 3: Workflow Step Execution (JVNAUTOSCI-803)

```python
class WorkflowStep:
    def __init__(self, prompt: str, tools: List[ToolDefinition]):
        self.prompt = prompt
        self.tools = tools

    async def execute(self) -> dict:
        """Execute workflow step with tool calling"""

        response = await client.generate_with_tools(
            prompt=self.prompt,
            available_tools=self.tools,
        )

        # Create execution trace (IMPORTANT: Include call_id)
        trace = {
            "step_id": id(self),
            "timestamp": datetime.now(timezone.utc),
            "response": response.text_response,
            "tool_calls": [
                {
                    "call_id": tc.call_id,           # ← Use for correlation
                    "tool_name": tc.tool_name,
                    "payload": tc.payload,
                }
                for tc in response.tool_calls
            ],
        }

        return trace
```

## Troubleshooting

### "Tool not called"
1. Check tool is in available_tools list
2. Check tool description is clear
3. Check prompt explicitly requests tool usage
4. Try increasing temperature (more creative)

### "Tool name validation error"
1. Tool name must match exactly (case-sensitive)
2. Tool must be in available_tools list
3. Fallback: set available_tools=[] for text-only response

### "JSON parsing error" (Ollama)
1. Check OllamaClient is being used (for local models)
2. Verify JSON format in constrained prompt
3. Try simpler schemas
4. Check Ollama server is running

### "Token limit exceeded"
1. Increase max_tokens in config
2. Reduce context/history length
3. Simplify tool descriptions
4. Use shorter model names

## Testing

```python
# Test with mock tools
def test_tool_calling():
    config = LLMClientConfig(model="gpt-4")
    client = get_llm_client(config)

    tools = [ToolDefinition(name="test", description="...", input_schema={...})]

    response = client.generate_with_tools_sync(
        prompt="Use the test tool",
        available_tools=tools,
    )

    assert response is not None
    assert response.tool_calls is not None
```

## Performance Tips

1. **Reuse client instances**: Create once, use many times
2. **Batch tool definitions**: Define tools once, pass to multiple requests
3. **Use async for concurrency**: Multiple concurrent generate_with_tools() calls
4. **Cache tool definitions**: Don't recreate ToolDefinition objects each time
5. **Monitor token usage**: Check response.usage for optimization opportunities

## API Reference

### Functions

| Function | Signature | Returns |
|----------|-----------|---------|
| `get_llm_client()` | `get_llm_client(config: LLMClientConfig) -> LLMClient` | Client instance |

### Classes

| Class | Purpose |
|-------|---------|
| `LLMClientConfig` | Configuration (model, api_key, temperature, etc.) |
| `LLMClient` | Abstract base class (async generate_with_tools) |
| `ToolDefinition` | Tool metadata (name, description, schemas) |
| `ToolCall` | Tool invocation (name, payload, call_id) |
| `LLMResponse` | Response (text, tool_calls, model, usage) |
| `ToolCallError` | Exception for tool calling failures |

### Provider Classes

| Provider | Class | Models |
|----------|-------|--------|
| OpenAI | `OpenAIClient` | gpt-4, gpt-3.5-turbo |
| Gemini | `GeminiClient` | gemini-pro, gemini-1.5-pro |
| Ollama | `OllamaClient` | llama2, mistral, neural-chat, etc. |

## Environment Variables

```
OPENAI_API_KEY              # OpenAI API key (if not in config)
GOOGLE_API_KEY              # Google API key (if not in config)
VON_INTERNAL_MCP_STRUCTURED_TOOL_CALLING   # Enable/disable (default: 1)
VON_LEGACY_JSON_TEXT_PARSING                # Force legacy (default: 0)
```

## Links

- [Full Guide](structured_tool_calling_guide.md)
- [Implementation Roadmap](jvnautosci_799_implementation_roadmap.md)
- [Phase 0 Audit](jvnautosci_799_phase0_audit.md)
- [Phases 1-2 Summary](jvnautosci_799_phases_1-2_summary.md)
- JIRA: JVNAUTOSCI-799
- Related: JVNAUTOSCI-803 (LLM Workflows)

---

For questions or issues, see the full guide or the docstrings in the source code.
