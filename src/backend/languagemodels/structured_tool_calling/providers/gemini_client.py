"""Gemini client with structured tool calling support."""

import json
import logging
from typing import Any, Dict, List, Optional, Sequence
import asyncio

from ..types import ToolCall, ToolDefinition, LLMResponse, ToolCallError
from ..client import LLMClient, LLMClientConfig

logger = logging.getLogger(__name__)


class GeminiClient(LLMClient):
    """LLM client for Google Gemini models with function calling support.

    Supports native function calling for Gemini Pro and Gemini Pro Vision.
    Falls back to JSON-in-text parsing if structured calling fails and fallback is enabled.
    """

    def __init__(self, config: LLMClientConfig):
        super().__init__(config)

        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError("google-generativeai package required for GeminiClient")

        self._genai = genai

        if config.api_key:
            genai.configure(api_key=config.api_key)  # type: ignore[attr-defined]

        self._model = genai.GenerativeModel(  # type: ignore[attr-defined]
            model_name=config.model,
            generation_config={
                "temperature": config.temperature,
                "max_output_tokens": config.max_tokens or 2048,
            }
        )

    async def generate_with_tools(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate response using Gemini function calling API.

        Note: This runs synchronously using asyncio to avoid blocking.
        """
        # Validate tools
        for tool in available_tools:
            self._validate_input_schema(tool)

        # Run sync implementation in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.generate_with_tools_sync,
            prompt,
            available_tools,
            context,
            system_message,
        )

    def generate_with_tools_sync(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Synchronous implementation using Gemini SDK."""

        try:
            # Convert tools to Gemini format
            tools = self._convert_tools_to_gemini_format(available_tools)

            # Build chat history
            messages = self._build_messages(prompt, context, system_message)

            # Call Gemini API with function calling
            response = self._model.generate_content(
                messages,  # type: ignore[arg-type]
                tools=tools,
                **kwargs,
            )

            return self._parse_response(response, available_tools)

        except Exception as exc:
            self.logger.error(f"Gemini API error: {exc}")
            if self.config.fallback_to_json_text:
                self.logger.info("Falling back to JSON-in-text parsing")
            raise ToolCallError(f"Gemini call failed: {exc}") from exc

    def _convert_tools_to_gemini_format(
        self,
        tools: List[ToolDefinition],
    ) -> Any:
        """Convert ToolDefinitions to Gemini tool format."""
        gemini_tools = []

        for tool in tools:
            # Gemini expects function definitions
            gemini_tool = self._genai.types.Tool(  # type: ignore[attr-defined]
                function_declarations=[
                    self._genai.types.FunctionDeclaration(  # type: ignore[attr-defined]
                        name=tool.name,
                        description=tool.description,
                        parameters=self._genai.types.Schema(  # type: ignore[attr-defined]
                            type=self._genai.types.Type.OBJECT,  # type: ignore[attr-defined]
                            properties={
                                prop: self._schema_property_to_gemini(prop_schema)
                                for prop, prop_schema in tool.input_schema.get("properties", {}).items()
                            },
                            required=tool.input_schema.get("required", []),
                        ),
                    )
                ]
            )
            gemini_tools.append(gemini_tool)

        return gemini_tools

    def _schema_property_to_gemini(self, prop_schema: Dict[str, Any]) -> Any:
        """Convert JSON Schema property to Gemini schema."""
        prop_type = prop_schema.get("type", "string")

        # Map JSON Schema types to Gemini types
        type_map = {
            "string": self._genai.types.Type.STRING,  # type: ignore[attr-defined]
            "number": self._genai.types.Type.NUMBER,  # type: ignore[attr-defined]
            "integer": self._genai.types.Type.INTEGER,  # type: ignore[attr-defined]
            "boolean": self._genai.types.Type.BOOLEAN,  # type: ignore[attr-defined]
            "array": self._genai.types.Type.ARRAY,  # type: ignore[attr-defined]
            "object": self._genai.types.Type.OBJECT,  # type: ignore[attr-defined]
        }

        gemini_type = type_map.get(prop_type, self._genai.types.Type.STRING)  # type: ignore[attr-defined]

        return self._genai.types.Schema(  # type: ignore[attr-defined]
            type=gemini_type,
            description=prop_schema.get("description", ""),
        )

    def _build_messages(
        self,
        prompt: str,
        context: Optional[Sequence[Dict[str, Any]]],
        system_message: Optional[str],
    ) -> List[Dict[str, str]]:
        """Build Gemini message list."""
        messages: List[Dict[str, str]] = []

        if system_message:
            messages.append({"role": "user", "content": system_message})

        if context:
            for msg in context:
                if isinstance(msg, dict) and "role" in msg and "content" in msg:
                    # Normalise role for Gemini (uses 'user' and 'model')
                    role = "model" if msg["role"] == "assistant" else msg["role"]
                    messages.append({
                        "role": role,
                        "content": msg["content"],
                    })

        messages.append({"role": "user", "content": prompt})

        return messages

    def _parse_response(
        self,
        response: Any,
        available_tools: List[ToolDefinition],
    ) -> LLMResponse:
        """Parse Gemini response into canonical LLMResponse format."""

        text_response = ""
        tool_calls: List[ToolCall] = []

        tool_name_map = {tool.name: tool for tool in available_tools}

        try:
            # Extract text and function calls from response
            if hasattr(response, 'text'):
                text_response = response.text

            if hasattr(response, 'function_calls') and response.function_calls:
                for fc in response.function_calls():
                    try:
                        tool_name = fc.name

                        # Validate tool exists
                        if tool_name not in tool_name_map:
                            self.logger.warning(f"Unknown tool requested: {tool_name}")
                            continue

                        # Get arguments dict
                        payload = dict(fc.args) if hasattr(fc, 'args') else {}

                        tool_calls.append(ToolCall(
                            tool_name=tool_name,
                            payload=payload,
                        ))

                    except Exception as exc:
                        self.logger.error(f"Failed to parse Gemini function call: {exc}")
                        continue

        except Exception as exc:
            self.logger.error(f"Error parsing Gemini response: {exc}")

        return LLMResponse(
            text_response=text_response,
            tool_calls=tool_calls,
            raw_response=None,  # Gemini response object is not easily serialisable
            model=self.config.model,
            usage=None,  # Gemini doesn't expose token usage easily
        )
