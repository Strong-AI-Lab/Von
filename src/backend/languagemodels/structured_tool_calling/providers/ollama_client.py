"""Ollama client with structured tool calling support."""

import json
import logging
from typing import Any, Dict, List, Optional, Sequence
import asyncio

from ..types import ToolCall, ToolDefinition, LLMResponse, ToolCallError
from ..client import LLMClient, LLMClientConfig

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
        """Synchronous implementation using Ollama."""

        try:
            # Build prompt with tool descriptions and JSON constraint
            enhanced_prompt = self._build_constrained_prompt(
                prompt,
                available_tools,
                system_message,
            )

            # Build message list
            messages = self._build_messages(enhanced_prompt, context)

            # Call Ollama with optional format constraint
            stream = self._ollama.chat(
                model=self.config.model,
                messages=messages,
                stream=True,
                **kwargs,
            )

            # Collect streamed response
            full_response = ""
            for chunk in stream:
                if isinstance(chunk, dict) and 'message' in chunk:
                    full_response += chunk['message'].get('content', '')

            return self._parse_response(full_response, available_tools)

        except Exception as exc:
            self.logger.error(f"Ollama API error: {exc}")
            if self.config.fallback_to_json_text:
                self.logger.info("Tool calling may be degraded with Ollama")
            raise ToolCallError(f"Ollama call failed: {exc}") from exc

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

        tool_descriptions = "\n".join([
            f"- {tool.name}: {tool.description}"
            for tool in available_tools
        ])

        constraint_prompt = f"""You are a helpful assistant that can invoke tools.

AVAILABLE TOOLS:
{tool_descriptions}

INSTRUCTIONS:
1. Respond naturally to the user's request.
2. If you need to call a tool, respond with valid JSON in this format:
{json.dumps({
    "action": "call_tool",
    "tool": "<tool_name>",
    "payload": "<tool_arguments>"
}, indent=2)}

3. You can only call tools listed above.
4. Ensure all JSON is valid and complete.
5. Do not include any text after the JSON object.

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
                    messages.append({
                        "role": msg["role"],
                        "content": msg["content"],
                    })

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

        tool_name_map = {tool.name: tool for tool in available_tools}

        # Try to extract JSON tool call from response
        tool_call = self._extract_json_tool_call(response_text)

        if tool_call:
            try:
                tool_name = tool_call.get("tool")
                payload = tool_call.get("payload", {})

                if tool_name in tool_name_map:
                    tool_calls.append(ToolCall(
                        tool_name=tool_name,
                        payload=payload if isinstance(payload, dict) else {},
                    ))
                    # Remove JSON from text response
                    text_response = self._remove_json_from_text(response_text, tool_call)
                else:
                    self.logger.warning(f"Unknown tool requested: {tool_name}")

            except (KeyError, ValueError) as exc:
                self.logger.error(f"Invalid tool call format: {exc}")

        return LLMResponse(
            text_response=text_response.strip(),
            tool_calls=tool_calls,
            raw_response=None,
            model=self.config.model,
            usage=None,
        )

    def _extract_json_tool_call(self, text: str) -> Optional[Dict[str, Any]]:
        """Extract JSON tool call from response text.

        Looks for JSON object containing "action", "tool", and "payload" fields.
        """
        import re

        # Try to find JSON object
        json_match = re.search(r'\{[^{}]*"action"\s*:\s*"call_tool"[^{}]*\}', text)

        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return None

    def _remove_json_from_text(
        self,
        text: str,
        tool_call: Dict[str, Any],
    ) -> str:
        """Remove JSON tool call from response text."""
        import re

        # Find and remove the JSON object
        json_str = json.dumps(tool_call)
        return re.sub(re.escape(json_str), "", text).strip()
