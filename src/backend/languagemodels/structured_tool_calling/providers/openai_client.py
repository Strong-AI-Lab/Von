"""OpenAI client with structured tool calling support."""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence
import asyncio

import openai

from ..types import ToolCall, ToolDefinition, LLMResponse, ToolCallError
from ..client import (
    LLMClient,
    LLMClientConfig,
    resolve_safe_temperature_for_model,
)
from ....services.model_parameter_service import (
    openai_chat_completions_kwargs_from_model_parameters,
)
from ....integrations.internal_mcp.tool_call_contracts import (
    validation_diagnostic,
)


logger = logging.getLogger(__name__)


class OpenAIClient(LLMClient):
    """LLM client for OpenAI models with native function calling support.

    Uses OpenAI's native function calling API (available in GPT-4, GPT-4 Turbo, GPT-3.5 Turbo).
    Falls back to JSON-in-text parsing if structured calling fails and fallback is enabled.
    """

    def __init__(self, config: LLMClientConfig):
        super().__init__(config)

        # Initialise OpenAI client
        kwargs: Dict[str, Any] = {}
        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url

        self._client = openai.AsyncOpenAI(**kwargs)
        self._sync_client = openai.OpenAI(**kwargs)

    async def generate_with_tools(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate response using OpenAI function calling API."""

        # Validate tools
        for tool in available_tools:
            self._validate_input_schema(tool)

        # Build message list
        messages = self._build_messages(prompt, context, system_message)

        # Convert tools to OpenAI format
        tools = [self._tool_definition_to_dict(tool) for tool in available_tools]

        try:
            request_kwargs = dict(kwargs)
            request_model = request_kwargs.pop("model", None) or self.config.model
            llm_params = request_kwargs.pop("llm_params", None)
            if self.config.max_tokens is not None:
                request_kwargs["max_tokens"] = self.config.max_tokens

            if isinstance(llm_params, dict) and llm_params:
                request_kwargs.update(
                    openai_chat_completions_kwargs_from_model_parameters(
                        llm_params,
                        model=request_model,
                    )
                )
            if (
                tools
                and "reasoning_effort" in request_kwargs
                and request_kwargs.get("reasoning_effort") != "none"
            ):
                request_kwargs.pop("reasoning_effort", None)
                logger.info(
                    "Omitting non-zero reasoning_effort for OpenAI Chat "
                    "Completions structured tool call; stored model effort "
                    "remains available on compatible API surfaces."
                )

            # Only pass temperature if the model supports it and a value is set
            # Some models (e.g. gpt-5.2) only accept default temperature
            safe_temperature = resolve_safe_temperature_for_model(
                self.config.model,
                self.config.temperature,
            )
            if safe_temperature is not None:
                request_kwargs["temperature"] = safe_temperature

            response = await self._client.chat.completions.create(
                model=request_model,
                messages=messages,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                **request_kwargs,
            )

            return self._parse_response(response, available_tools)

        except Exception as exc:
            self.logger.error(f"OpenAI API error: {exc}")
            if self.config.fallback_to_json_text:
                self.logger.info("Falling back to JSON-in-text parsing")
                # Fallback would be implemented here if needed
            raise ToolCallError(f"OpenAI call failed: {exc}") from exc

    def generate_with_tools_sync(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Synchronous wrapper using asyncio.run()."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                self.generate_with_tools(
                    prompt, available_tools, context, system_message, **kwargs
                )
            )
        finally:
            loop.close()

    def _build_messages(
        self,
        prompt: str,
        context: Optional[Sequence[Dict[str, Any]]],
        system_message: Optional[str],
    ) -> List[Dict[str, str]]:
        """Build OpenAI message list from prompt, context, and system message."""
        messages: List[Dict[str, str]] = []

        if system_message:
            messages.append({"role": "system", "content": system_message})

        if context:
            for msg in context:
                if isinstance(msg, dict) and "role" in msg and "content" in msg:
                    role = msg["role"]
                    if role in ("tool", "model"):
                        role = "assistant"
                    if role not in ("system", "user", "assistant"):
                        role = "user"
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
        """Parse OpenAI response into canonical LLMResponse format."""

        text_response = ""
        tool_calls: List[ToolCall] = []
        tool_call_diagnostics: List[Dict[str, Any]] = []

        # Process response content
        if response.choices and len(response.choices) > 0:
            choice = response.choices[0]

            # Extract text if present
            if choice.message.content:
                text_response = choice.message.content

            # Extract tool calls if present
            if choice.message.tool_calls:
                tool_name_map = {tool.name: tool for tool in available_tools}

                for tc in choice.message.tool_calls:
                    tool_name: str | None = None
                    try:
                        # Parse OpenAI function call format
                        tool_name = tc.function.name
                        payload_str = tc.function.arguments
                        payload = (
                            json.loads(payload_str)
                            if isinstance(payload_str, str)
                            else payload_str
                        )

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
                        if not isinstance(payload, dict):
                            message = (
                                f"Tool '{tool_name}' arguments must decode to a JSON object."
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
                                call_id=(
                                    tc.id
                                    if hasattr(tc, "id") and tc.id
                                    else str(uuid.uuid4())
                                ),
                            )
                        )

                    except (json.JSONDecodeError, AttributeError) as exc:
                        message = f"Failed to parse tool call: {exc}"
                        self.logger.error(message)
                        tool_call_diagnostics.append(
                            validation_diagnostic(
                                tool=tool_name,
                                error_code="provider_tool_call_parse_error",
                                message=message,
                            )
                        )
                        continue

        # Build usage info
        usage: Optional[Dict[str, Any]] = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(
            text_response=text_response,
            tool_calls=tool_calls,
            raw_response=(
                response.model_dump() if hasattr(response, "model_dump") else None
            ),
            model=response.model,
            usage=usage,
            tool_call_diagnostics=tool_call_diagnostics,
        )
