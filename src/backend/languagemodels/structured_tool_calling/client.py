"""Abstract interface for LLM clients with structured tool calling.

All LLM provider implementations (OpenAI, Gemini, Ollama) inherit from LLMClient.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union
import logging

from .types import ToolDefinition, LLMResponse


logger = logging.getLogger(__name__)


# Models that only support default temperature (1.0) - JVNAUTOSCI-1010
# See: OpenAI API error "temperature does not support X with this model"
MODELS_REQUIRING_DEFAULT_TEMPERATURE = frozenset(
    {
        "gpt-5.2-chat-latest",
        "gpt-5.2",
    }
)


def model_supports_custom_temperature(model: str) -> bool:
    """Check if a model supports custom temperature values.

    Some newer OpenAI models (e.g. gpt-5.2) only accept temperature=1.
    """
    if not model:
        return True
    model_lower = model.lower()
    # Check exact match first
    if model_lower in MODELS_REQUIRING_DEFAULT_TEMPERATURE:
        return False
    # Check prefix match for version variants
    for restricted in MODELS_REQUIRING_DEFAULT_TEMPERATURE:
        if model_lower.startswith(restricted):
            return False
    return True


@dataclass
class LLMClientConfig:
    """Configuration for LLM client instantiation.

    Attributes:
        model: Model identifier (e.g., 'gpt-4', 'gemini-pro', 'llama2')
        api_key: Authentication token (if required)
        base_url: API endpoint URL (for self-hosted or proxy scenarios)
        temperature: Sampling temperature (0-2 typical range). None = use model default.
        max_tokens: Maximum tokens to generate
        enable_structured_calling: Use provider-native structured calling (default True)
        fallback_to_json_text: Allow JSON-in-text parsing if structured calling fails (default True)
    """

    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    enable_structured_calling: bool = True
    fallback_to_json_text: bool = True


class LLMClient(ABC):
    """Abstract base for LLM clients with structured tool calling support.

    Subclasses must implement generate_with_tools() to support their specific
    provider's API (OpenAI functions, Gemini function calling, etc.).

    This interface is designed to be consumed by:
    - InternalMCPChatOrchestrator (tool invocation loop)
    - Workflow engine step executors (JVNAUTOSCI-803)
    - Annotation extraction service
    - Any other LLM operation requiring tool calling
    """

    def __init__(self, config: LLMClientConfig):
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    async def generate_with_tools(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate a response using structured tool calling.

        Args:
            prompt: User message to send to LLM
            available_tools: List of tools the model can invoke
            context: Conversation history (list of {role, content} dicts)
            system_message: System prompt overriding default
            **kwargs: Provider-specific options

        Returns:
            LLMResponse with either text_response or tool_calls (or both)

        Raises:
            ToolCallError: If tool calling fails and fallback is disabled
        """
        pass

    @abstractmethod
    def generate_with_tools_sync(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Synchronous wrapper for generate_with_tools().

        Implementations should call the async version if possible, or provide
        a blocking implementation. Used for backward compatibility and
        orchestrator integration.
        """
        pass

    def _tool_definition_to_dict(self, tool: ToolDefinition) -> Dict[str, Any]:
        """Convert ToolDefinition to provider-specific format (base implementation).

        Subclasses can override for provider-specific schema mapping.

        Returns standard OpenAI function calling format:
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema
            }
        }
        """
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    def _validate_input_schema(self, tool: ToolDefinition) -> None:
        """Validate that input_schema is valid JSON Schema.

        Raises ValueError if schema is invalid.
        """
        # Basic validation: must have 'type' or 'properties'
        schema = tool.input_schema
        if not isinstance(schema, dict):
            raise ValueError(f"Tool {tool.name}: input_schema must be a dict")

        # Allow either "type": "object" or direct "properties"
        has_type = "type" in schema
        has_props = "properties" in schema

        if not (has_type or has_props):
            raise ValueError(
                f"Tool {tool.name}: input_schema must have 'type' or 'properties'"
            )
