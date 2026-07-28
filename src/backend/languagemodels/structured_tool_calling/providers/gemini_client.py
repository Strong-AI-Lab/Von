"""Gemini client with structured tool calling support."""

import asyncio
import logging
import math
from time import monotonic
from typing import Any, Dict, List, Optional, Sequence

from ..types import ToolCall, ToolDefinition, LLMResponse, ToolCallError
from ..client import (
    LLMClient,
    LLMClientConfig,
    split_request_timeout_from_llm_params,
)
from ....integrations.internal_mcp.tool_call_contracts import validation_diagnostic

logger = logging.getLogger(__name__)


class GeminiClient(LLMClient):
    """LLM client for Google Gemini models with function calling support.

    Supports native function calling for Gemini Pro and Gemini Pro Vision.
    Falls back to JSON-in-text parsing if structured calling fails and fallback is enabled.
    """

    def __init__(self, config: LLMClientConfig):
        super().__init__(config)

        try:
            from google import genai
        except ImportError:
            raise ImportError("google-genai package required for GeminiClient")

        self._genai = genai

        # Create client with API key if provided
        client_kwargs: dict[str, Any] = {}
        if config.api_key:
            client_kwargs["api_key"] = config.api_key

        self._client_kwargs = client_kwargs
        self._client = genai.Client(**client_kwargs)  # type: ignore[attr-defined]
        self._model_name = config.model
        self._temperature = config.temperature
        self._max_tokens = config.max_tokens or 2048

    async def generate_with_tools(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate response using Gemini's native asynchronous client."""

        for tool in available_tools:
            self._validate_input_schema(tool)

        request_kwargs = dict(kwargs)
        _, request_timeout_seconds = split_request_timeout_from_llm_params(
            request_kwargs.pop("llm_params", None)
        )
        request_deadline_monotonic = (
            monotonic() + request_timeout_seconds
            if request_timeout_seconds is not None
            else None
        )
        request_client = self._client
        owns_request_client = False

        try:
            tools = self._convert_tools_to_gemini_format(available_tools)
            messages = self._build_messages(prompt, context, system_message)
            config = self._genai.types.GenerateContentConfig(
                temperature=self._temperature,
                max_output_tokens=self._max_tokens,
                tools=tools,
            )

            if request_deadline_monotonic is not None:
                remaining_seconds = request_deadline_monotonic - monotonic()
                if remaining_seconds <= 0.0:
                    raise TimeoutError(
                        "Gemini structured-tool request deadline exhausted."
                    )
                request_client = self._genai.Client(
                    **self._client_kwargs,
                    http_options=self._genai.types.HttpOptions(
                        timeout=max(1, math.ceil(remaining_seconds * 1000.0))
                    ),
                )
                owns_request_client = True

            async def _request() -> Any:
                return await request_client.aio.models.generate_content(
                    model=self._model_name,
                    contents=messages,  # type: ignore[arg-type]
                    config=config,
                )

            if request_deadline_monotonic is None:
                response = await _request()
            else:
                remaining_seconds = request_deadline_monotonic - monotonic()
                if remaining_seconds <= 0.0:
                    raise TimeoutError(
                        "Gemini structured-tool request deadline exhausted."
                    )
                async with asyncio.timeout(remaining_seconds):
                    response = await _request()
            return self._parse_response(response, available_tools)
        except TimeoutError as exc:
            self.logger.error("Gemini structured-tool request deadline exhausted.")
            raise ToolCallError(
                "Gemini structured-tool request deadline exhausted."
            ) from exc
        except Exception as exc:
            self.logger.error("Gemini API error: %s", exc)
            if self.config.fallback_to_json_text:
                self.logger.info("Falling back to JSON-in-text parsing")
            raise ToolCallError(f"Gemini call failed: {exc}") from exc
        finally:
            if owns_request_client:
                await request_client.aio.aclose()

    def generate_with_tools_sync(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Synchronous wrapper around the same bounded transport."""

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                self.generate_with_tools(
                    prompt,
                    available_tools,
                    context,
                    system_message,
                    **kwargs,
                )
            )
        finally:
            loop.close()

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
                                for prop, prop_schema in tool.input_schema.get(
                                    "properties", {}
                                ).items()
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
                    messages.append(
                        {
                            "role": role,
                            "content": msg["content"],
                        }
                    )

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
        tool_call_diagnostics: List[Dict[str, Any]] = []

        tool_name_map = {tool.name: tool for tool in available_tools}

        try:
            # Extract text and function calls from response
            if hasattr(response, "text"):
                text_response = response.text

            if hasattr(response, "function_calls") and response.function_calls:
                for fc in response.function_calls():
                    try:
                        tool_name = fc.name

                        # Validate tool exists
                        if tool_name not in tool_name_map:
                            message = f"Unknown tool requested: {tool_name}"
                            self.logger.warning(message)
                            tool_call_diagnostics.append(
                                validation_diagnostic(
                                    tool=tool_name,
                                    error_code="unknown_tool",
                                    message=message,
                                )
                            )
                            continue

                        # Get arguments dict
                        payload = dict(fc.args) if hasattr(fc, "args") else {}
                        if not isinstance(payload, dict):
                            message = (
                                f"Tool '{tool_name}' arguments must be a JSON object."
                            )
                            self.logger.error(message)
                            tool_call_diagnostics.append(
                                validation_diagnostic(
                                    tool=tool_name,
                                    error_code="arguments_not_object",
                                    message=message,
                                )
                            )
                            continue

                        tool_calls.append(
                            ToolCall(
                                tool_name=tool_name,
                                payload=payload,
                            )
                        )

                    except Exception as exc:
                        message = f"Failed to parse Gemini function call: {exc}"
                        self.logger.error(message)
                        tool_call_diagnostics.append(
                            validation_diagnostic(
                                tool=None,
                                error_code="provider_tool_call_parse_error",
                                message=message,
                            )
                        )
                        continue

        except Exception as exc:
            message = f"Error parsing Gemini response: {exc}"
            self.logger.error(message)
            tool_call_diagnostics.append(
                validation_diagnostic(
                    tool=None,
                    error_code="provider_response_parse_error",
                    message=message,
                )
            )

        return LLMResponse(
            text_response=text_response,
            tool_calls=tool_calls,
            raw_response=None,  # Gemini response object is not easily serialisable
            model=self.config.model,
            usage=None,  # Gemini doesn't expose token usage easily
            tool_call_diagnostics=tool_call_diagnostics,
        )
