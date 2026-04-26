"""Abstract interface for LLM clients with structured tool calling.

All LLM provider implementations (OpenAI, Gemini, Ollama) inherit from LLMClient.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
import logging

from ...services.model_registry_service import (
    resolve_model_parameter_policy,
    sanitise_model_parameter_value,
)
from ...integrations.internal_mcp.tool_call_contracts import (
    is_strict_tool_schema_compatible,
    strip_internal_schema_extensions,
)
from .types import ToolDefinition, LLMResponse


logger = logging.getLogger(__name__)


def model_supports_custom_temperature(model: str) -> bool:
    """Return whether the KB allows explicit temperature for this model."""

    if not isinstance(model, str) or not model.strip():
        return True

    policy = resolve_model_parameter_policy(
        model=model,
        provider="openai",
        parameter="temperature",
        api_surface="chat_completions",
    )
    if not isinstance(policy, dict):
        return True

    action = str(policy.get("action") or "").strip().lower().replace("-", "_")
    return action not in {"omit", "fixed_value"}


def resolve_safe_temperature_for_model(
    model: str,
    temperature: Optional[float],
) -> Optional[float]:
    """Return a temperature that is safe to send for the given model.

    Runtime parameter policies are resolved from the model registry in Vontology.
    When a model/API profile rejects explicit temperature values, return ``None``
    so callers omit the parameter and allow the provider default to apply.
    """

    if temperature is None:
        return None
    sanitised = sanitise_model_parameter_value(
        model=model,
        provider="openai",
        parameter="temperature",
        value=temperature,
        api_surface="chat_completions",
    )
    return sanitised if isinstance(sanitised, (int, float)) else None


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
        parameters = strip_internal_schema_extensions(tool.input_schema)
        function_payload: Dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        }
        if is_strict_tool_schema_compatible(parameters):
            function_payload["strict"] = True

        return {
            "type": "function",
            "function": function_payload,
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
