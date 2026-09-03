"""Ollama client with structured tool calling support."""

import asyncio
import json
import logging
from collections.abc import Mapping
from time import monotonic
from typing import Any, Dict, List, Optional, Sequence

from ..types import ToolCall, ToolDefinition, LLMResponse, ToolCallError
from ..client import (
    LLMClient,
    LLMClientConfig,
    observe_request_advisory,
    split_request_advisory_from_llm_params,
)
from ....integrations.internal_mcp.tool_call_contracts import validation_diagnostic

logger = logging.getLogger(__name__)


class OllamaClient(LLMClient):
    """LLM client for Ollama models with constrained generation and JSON fallback.

    Ollama doesn't natively support function calling like OpenAI or Gemini.
    Instead, this client uses:
    1. Grammar-based constrained generation (if available)
    2. JSON-in-text parsing with strict validation (fallback)

    This enables reliable tool calling for local/self-hosted models.
    """

    def __init__(self, config: LLMClientConfig):
        super().__init__(config)

        try:
            import ollama
        except ImportError:
            raise ImportError("ollama package required for OllamaClient")

        self._ollama = ollama
        self._base_url = config.base_url or "http://localhost:11434"

    async def generate_with_tools(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate response using Ollama with JSON constraint grammar."""

        for tool in available_tools:
            self._validate_input_schema(tool)

        request_kwargs = dict(kwargs)
        _, request_advisory_seconds = split_request_advisory_from_llm_params(
            request_kwargs.pop("llm_params", None)
        )
        request_started_monotonic = monotonic()
        request_client_kwargs: dict[str, Any] = {"host": self._base_url}
        request_client = self._ollama.AsyncClient(**request_client_kwargs)

        try:
            enhanced_prompt = self._build_constrained_prompt(
                prompt,
                available_tools,
                system_message,
            )
            messages = self._build_messages(enhanced_prompt, context)

            async def _request() -> str:
                stream = await request_client.chat(
                    model=self.config.model,
                    messages=messages,
                    stream=True,
                )
                full_response = ""
                async for chunk in stream:
                    message = (
                        chunk.get("message")
                        if isinstance(chunk, Mapping)
                        else getattr(chunk, "message", None)
                    )
                    content = (
                        message.get("content", "")
                        if isinstance(message, Mapping)
                        else getattr(message, "content", "")
                    )
                    full_response += str(content or "")
                return full_response

            full_response = await _request()

            observe_request_advisory(
                provider="ollama",
                advisory_seconds=request_advisory_seconds,
                started_monotonic=request_started_monotonic,
                event_logger=self.logger,
            )
            return self._parse_response(full_response, available_tools)
        except Exception as exc:
            self.logger.error("Ollama API error: %s", exc)
            if self.config.fallback_to_json_text:
                self.logger.info("Tool calling may be degraded with Ollama")
            raise ToolCallError(f"Ollama call failed: {exc}") from exc
        finally:
            await request_client.close()

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

    def _build_constrained_prompt(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        system_message: Optional[str],
    ) -> str:
        """Build prompt that guides model towards JSON tool calls.

        For Ollama models without grammar support, we provide strong
        instructions and examples to elicit proper JSON formatting.
        """

        tool_descriptions = "\n\n".join(
            [
                (
                    f"- {tool.name}: {tool.description}\n"
                    "  Input JSON schema: "
                    f"{json.dumps(tool.input_schema, sort_keys=True)}"
                )
                for tool in available_tools
            ]
        )

        constraint_prompt = f"""You are a helpful assistant that can invoke tools.

AVAILABLE TOOLS:
{tool_descriptions}

INSTRUCTIONS:
1. Respond naturally to the user's request.
2. If you need to call a tool, respond with valid JSON in this format:
{json.dumps({
    "action": "call_tool",
    "tool": "<tool_name>",
    "payload": {"<argument_name>": "<value>"}
}, indent=2)}

   If you need to call multiple tools, respond with a JSON array of tool calls
   or multiple JSON objects separated by newlines.

3. You can only call tools listed above.
4. The payload must be a JSON object that matches that tool's Input JSON schema.
   Copy argument names exactly and do not add undeclared fields.
5. Ensure all JSON is valid and complete.
6. Do not include any text after the JSON.

USER REQUEST:
{prompt}
"""

        if system_message:
            constraint_prompt = f"{system_message}\n\n{constraint_prompt}"

        return constraint_prompt

    def _build_messages(
        self,
        prompt: str,
        context: Optional[Sequence[Dict[str, Any]]],
    ) -> List[Dict[str, str]]:
        """Build Ollama message list."""
        messages: List[Dict[str, str]] = []

        if context:
            for msg in context:
                if isinstance(msg, dict) and "role" in msg and "content" in msg:
                    messages.append(
                        {
                            "role": msg["role"],
                            "content": msg["content"],
                        }
                    )

        messages.append({"role": "user", "content": prompt})

        return messages

    def _parse_response(
        self,
        response_text: str,
        available_tools: List[ToolDefinition],
    ) -> LLMResponse:
        """Parse Ollama response with JSON extraction and validation."""

        text_response = response_text
        tool_calls: List[ToolCall] = []
        tool_call_diagnostics: List[Dict[str, Any]] = []

        tool_name_map = {tool.name: tool for tool in available_tools}

        # Try to extract JSON tool calls from response
        tool_call_payloads = self._extract_json_tool_calls(response_text)

        if tool_call_payloads:
            for tool_call in tool_call_payloads:
                try:
                    tool_name = tool_call.get("tool")
                    payload = tool_call.get("payload", {})

                    if tool_name in tool_name_map:
                        if not isinstance(payload, dict):
                            message = (
                                f"Tool '{tool_name}' payload must be a JSON object."
                            )
                            self.logger.error(message)
                            tool_call_diagnostics.append(
                                validation_diagnostic(
                                    tool=tool_name,
                                    error_code="payload_not_object",
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
                    else:
                        message = f"Unknown tool requested: {tool_name}"
                        self.logger.warning(message)
                        tool_call_diagnostics.append(
                            validation_diagnostic(
                                tool=tool_name if isinstance(tool_name, str) else None,
                                error_code="unknown_tool",
                                message=message,
                            )
                        )

                except (KeyError, ValueError) as exc:
                    message = f"Invalid tool call format: {exc}"
                    self.logger.error(message)
                    tool_call_diagnostics.append(
                        validation_diagnostic(
                            tool=None,
                            error_code="provider_tool_call_parse_error",
                            message=message,
                        )
                    )

            # Remove JSON from text response
            text_response = self._remove_json_from_text(
                response_text, tool_call_payloads
            )

        return LLMResponse(
            text_response=text_response.strip(),
            tool_calls=tool_calls,
            raw_response=None,
            model=self.config.model,
            usage=None,
            tool_call_diagnostics=tool_call_diagnostics,
        )

    def _extract_json_tool_calls(self, text: str) -> List[Dict[str, Any]]:
        """Extract JSON tool calls from response text.

        Accepts a single JSON object, a JSON array of tool calls, or multiple
        JSON objects separated by whitespace.
        """
        calls: List[Dict[str, Any]] = []
        for _, _, decoded_calls in self._json_tool_call_segments(text):
            calls.extend(decoded_calls)

        return calls

    def _json_tool_call_segments(
        self, text: str
    ) -> List[tuple[int, int, List[Dict[str, Any]]]]:
        """Decode complete tool-call JSON values and retain their source spans.

        ``json.loads`` cannot consume consecutive top-level JSON objects, while
        the former regular-expression fallback could not cross nested payload
        objects. Ollama's constrained prompt explicitly permits both forms, so
        use ``raw_decode`` from each plausible JSON boundary instead.
        """

        decoder = json.JSONDecoder()
        segments: List[tuple[int, int, List[Dict[str, Any]]]] = []
        cursor = 0

        while cursor < len(text):
            object_start = text.find("{", cursor)
            array_start = text.find("[", cursor)
            candidates = [index for index in (object_start, array_start) if index >= 0]
            if not candidates:
                break
            start = min(candidates)
            try:
                parsed, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                cursor = start + 1
                continue

            decoded_calls: List[Dict[str, Any]] = []
            if isinstance(parsed, dict) and parsed.get("action") == "call_tool":
                decoded_calls = [parsed]
            elif (
                isinstance(parsed, list)
                and parsed
                and all(
                    isinstance(item, dict) and item.get("action") == "call_tool"
                    for item in parsed
                )
            ):
                decoded_calls = list(parsed)

            if decoded_calls:
                segments.append((start, end, decoded_calls))
            cursor = max(end, start + 1)

        return segments

    def _remove_json_from_text(
        self,
        text: str,
        tool_calls: List[Dict[str, Any]],
    ) -> str:
        """Remove JSON tool calls from response text."""
        import re

        del tool_calls  # Source spans are exact; reconstructed JSON is not.
        segments = self._json_tool_call_segments(text)
        if not segments:
            return text.strip()

        parts: List[str] = []
        cursor = 0
        for start, end, _ in segments:
            parts.append(text[cursor:start])
            cursor = end
        parts.append(text[cursor:])
        cleaned = "".join(parts)
        cleaned = re.sub(r"```(?:json)?\s*```", "", cleaned, flags=re.IGNORECASE)

        return cleaned.strip()
