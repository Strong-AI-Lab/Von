"""Gemini provider-native structured tool transport.

Gemini 3.7 runs on the Interactions API with ``store=False``.  Its opaque
thought/function steps are retained in :class:`LLMContinuation` and replayed
exactly on the next stateless request.  Older models retain GenerateContent as
a compatibility surface, including exact model-content replay.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from time import monotonic
from typing import Any
from uuid import uuid4

from ....integrations.internal_mcp.tool_call_contracts import (
    strip_internal_schema_extensions,
    validation_diagnostic,
)
from ....services.model_parameter_service import gemini_kwargs_from_model_parameters
from ..client import (
    LLMClient,
    LLMClientConfig,
    observe_request_advisory,
    split_request_advisory_from_llm_params,
)
from ..transport import (
    sanitise_transport_telemetry_text,
    sanitise_transport_telemetry_value,
)
from ..types import (
    LLMContinuation,
    LLMResponse,
    StructuredToolProtocolError,
    StructuredToolTransportError,
    ToolCall,
    ToolCallError,
    ToolDefinition,
    ToolResult,
)

GEMINI_INTERACTIONS_SURFACE = "interactions"
GEMINI_GENERATE_CONTENT_SURFACE = "gemini_generate_content"


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _provider_mapping(value: Any) -> dict[str, Any]:
    """Return provider data in JSON form, without SDK HTTP response internals."""

    if isinstance(value, Mapping):
        return dict(value)
    dumper = getattr(value, "model_dump", None)
    if not callable(dumper):
        return {}
    try:
        result = dumper(mode="json", exclude_none=True)
    except TypeError:  # google-genai 1.x compatibility
        result = dumper(exclude_none=True)
    return dict(result) if isinstance(result, Mapping) else {}


def _provider_error_evidence(value: Any) -> list[dict[str, Any]]:
    """Return bounded, credential-free provider errors without SDK internals."""

    if value is None:
        return []
    raw_items = (
        list(value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
        else [value]
    )
    evidence: list[dict[str, Any]] = []
    for item in raw_items[:10]:
        mapping = _provider_mapping(item)
        if mapping:
            sanitised = sanitise_transport_telemetry_value(
                mapping,
                key="provider_error",
            )
            if isinstance(sanitised, Mapping):
                evidence.append(dict(sanitised))
            continue
        if isinstance(item, str) and item.strip():
            evidence.append(
                {"message": sanitise_transport_telemetry_text(item.strip())[:512]}
            )
        else:
            evidence.append({"error": "provider_error_details_unavailable"})
    return evidence


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, default=str, separators=(",", ":"))


def _is_gemini_37_flash(model: str) -> bool:
    model_id = str(model or "").strip().lower()
    if model_id.startswith("models/"):
        model_id = model_id.split("/", 1)[1]
    return model_id == "gemini-3.7-flash" or model_id.startswith("gemini-3.7-flash-")


class GeminiClient(LLMClient):
    """Gemini client with exact provider tool-call correlation."""

    def __init__(self, config: LLMClientConfig):
        super().__init__(config)
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover
            raise ImportError("google-genai package required for GeminiClient") from exc

        self._genai = genai
        self._client_kwargs: dict[str, Any] = {}
        if config.api_key:
            self._client_kwargs["api_key"] = config.api_key
        self._client = genai.Client(**self._client_kwargs)
        self._model_name = config.model
        self._temperature = config.temperature
        self._max_tokens = config.max_tokens or 2048
        self._assert_interactions_available(self._client, self._model_name)

    @staticmethod
    def _assert_interactions_available(client: Any, model: str) -> None:
        if (
            _is_gemini_37_flash(model)
            and getattr(getattr(client, "aio", None), "interactions", None) is None
        ):
            raise ImportError(
                "Gemini 3.7 structured tools require a google-genai release "
                "whose Client exposes client.aio.interactions."
            )

    async def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[ToolDefinition],
        context: Sequence[dict[str, Any]] | None = None,
        system_message: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        for tool in available_tools:
            self._validate_input_schema(tool)

        request_kwargs = dict(kwargs)
        request_client = request_kwargs.pop("_request_client", self._client)
        request_model = str(request_kwargs.pop("model", None) or self._model_name)
        llm_params, advisory_seconds = split_request_advisory_from_llm_params(
            request_kwargs.pop("llm_params", None)
        )
        raw_continuation = request_kwargs.pop("continuation", None)
        continuation = LLMContinuation.from_value(raw_continuation)
        if raw_continuation is not None and continuation is None:
            raise StructuredToolProtocolError(
                "Structured continuation payload is malformed."
            )
        tool_results = self._coerce_tool_results(
            request_kwargs.pop("tool_results", None)
        )
        if tool_results and continuation is None:
            raise StructuredToolProtocolError(
                "Structured tool results require a valid continuation payload."
            )
        surface = (
            GEMINI_INTERACTIONS_SURFACE
            if _is_gemini_37_flash(request_model)
            else GEMINI_GENERATE_CONTENT_SURFACE
        )
        if continuation is not None:
            self._validate_continuation(
                continuation,
                model=request_model,
                surface=surface,
                tool_results=tool_results,
            )

        started = monotonic()
        try:
            if surface == GEMINI_INTERACTIONS_SURFACE:
                self._assert_interactions_available(request_client, request_model)
                response = await self._generate_interaction(
                    prompt=prompt,
                    available_tools=available_tools,
                    context=context,
                    system_message=system_message,
                    request_model=request_model,
                    llm_params=llm_params,
                    continuation=continuation,
                    tool_results=tool_results,
                    request_client=request_client,
                )
            else:
                response = await self._generate_content(
                    prompt=prompt,
                    available_tools=available_tools,
                    context=context,
                    system_message=system_message,
                    request_model=request_model,
                    llm_params=llm_params,
                    continuation=continuation,
                    tool_results=tool_results,
                    request_client=request_client,
                )
            observe_request_advisory(
                provider="gemini",
                advisory_seconds=advisory_seconds,
                started_monotonic=started,
                event_logger=self.logger,
            )
            return response
        except StructuredToolTransportError:
            raise
        except Exception as exc:
            safe_error = sanitise_transport_telemetry_text(str(exc))
            self.logger.error("Gemini structured-tool API error: %s", safe_error)
            raise ToolCallError(f"Gemini call failed: {safe_error}") from exc

    def generate_with_tools_sync(
        self,
        prompt: str,
        available_tools: list[ToolDefinition],
        context: Sequence[dict[str, Any]] | None = None,
        system_message: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Use a fresh SDK transport for the isolated synchronous event loop."""

        request_client = self._genai.Client(**self._client_kwargs)

        async def _run() -> LLMResponse:
            try:
                return await self.generate_with_tools(
                    prompt,
                    available_tools,
                    context,
                    system_message,
                    _request_client=request_client,
                    **kwargs,
                )
            finally:
                close = getattr(getattr(request_client, "aio", None), "aclose", None)
                if callable(close):
                    await close()

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()

    async def _generate_interaction(
        self,
        *,
        prompt: str,
        available_tools: list[ToolDefinition],
        context: Sequence[dict[str, Any]] | None,
        system_message: str | None,
        request_model: str,
        llm_params: Mapping[str, Any],
        continuation: LLMContinuation | None,
        tool_results: list[ToolResult],
        request_client: Any,
    ) -> LLMResponse:
        if continuation is None:
            input_steps, context_system = self._interaction_input(prompt, context)
        else:
            context_system = []
            input_steps = [
                *[dict(item) for item in continuation.input_items],
                *[dict(item) for item in continuation.output_items],
                *self._interaction_function_results(continuation, tool_results),
            ]
            if prompt.strip():
                input_steps.append(self._text_step("user_input", prompt))

        projected = gemini_kwargs_from_model_parameters(
            llm_params,
            model=request_model,
            api_surface=GEMINI_INTERACTIONS_SURFACE,
        )
        generation_config: dict[str, Any] = {"max_output_tokens": self._max_tokens}
        projection = projected.get("generation_config")
        if isinstance(projection, Mapping):
            generation_config.update(dict(projection))
        instruction_parts = list(context_system)
        if isinstance(system_message, str) and system_message.strip():
            instruction_parts.append(system_message.strip())
        request: dict[str, Any] = {
            "model": request_model,
            "input": input_steps,
            "store": False,
            "generation_config": generation_config,
        }
        tools = [self._interaction_tool(tool) for tool in available_tools]
        if tools:
            request["tools"] = tools
        if instruction_parts:
            request["system_instruction"] = "\n\n".join(instruction_parts)
        interaction = await request_client.aio.interactions.create(**request)
        return self._parse_interaction(
            interaction,
            available_tools,
            model=request_model,
            input_steps=input_steps,
            effective_parameters=projected,
        )

    async def _generate_content(
        self,
        *,
        prompt: str,
        available_tools: list[ToolDefinition],
        context: Sequence[dict[str, Any]] | None,
        system_message: str | None,
        request_model: str,
        llm_params: Mapping[str, Any],
        continuation: LLMContinuation | None,
        tool_results: list[ToolResult],
        request_client: Any,
    ) -> LLMResponse:
        if continuation is None:
            contents, context_system = self._content_input(prompt, context)
            input_items = [self._dump_content(item) for item in contents]
        else:
            context_system = []
            input_items = [
                *[dict(item) for item in continuation.input_items],
                *[dict(item) for item in continuation.output_items],
            ]
            contents = [
                self._genai.types.Content.model_validate(item) for item in input_items
            ]
            contents.extend(self._content_function_results(continuation, tool_results))
            if prompt.strip():
                contents.append(self._content("user", prompt))

        projected = gemini_kwargs_from_model_parameters(
            llm_params,
            model=request_model,
            api_surface=GEMINI_GENERATE_CONTENT_SURFACE,
        )
        config_values: dict[str, Any] = {
            "max_output_tokens": self._max_tokens,
            "tools": self._generate_content_tools(available_tools),
            **projected,
        }
        if self._temperature is not None:
            config_values["temperature"] = self._temperature
        instruction_parts = list(context_system)
        if isinstance(system_message, str) and system_message.strip():
            instruction_parts.append(system_message.strip())
        if instruction_parts:
            config_values["system_instruction"] = "\n\n".join(instruction_parts)
        response = await request_client.aio.models.generate_content(
            model=request_model,
            contents=contents,
            config=self._genai.types.GenerateContentConfig(**config_values),
        )
        return self._parse_generate_content(
            response,
            available_tools,
            model=request_model,
            input_items=input_items,
            effective_parameters=projected,
        )

    @staticmethod
    def _text_step(step_type: str, text: str) -> dict[str, Any]:
        return {
            "type": step_type,
            "content": [{"type": "text", "text": text}],
        }

    def _interaction_input(
        self,
        prompt: str,
        context: Sequence[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        steps: list[dict[str, Any]] = []
        system_parts: list[str] = []
        for message in context or []:
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "user").strip().lower()
            content = _json_text(message.get("content", ""))
            if role == "system":
                if content.strip():
                    system_parts.append(content)
            elif role in {"assistant", "model"}:
                steps.append(self._text_step("model_output", content))
            elif role == "tool":
                steps.append(
                    self._text_step(
                        "user_input",
                        "Prior tool evidence (not provider continuation): " + content,
                    )
                )
            else:
                steps.append(self._text_step("user_input", content))
        steps.append(self._text_step("user_input", prompt))
        return steps, system_parts

    def _content_input(
        self,
        prompt: str,
        context: Sequence[dict[str, Any]] | None,
    ) -> tuple[list[Any], list[str]]:
        contents: list[Any] = []
        system_parts: list[str] = []
        for message in context or []:
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "user").strip().lower()
            content = _json_text(message.get("content", ""))
            if role == "system":
                if content.strip():
                    system_parts.append(content)
            elif role in {"assistant", "model"}:
                contents.append(self._content("model", content))
            elif role == "tool":
                contents.append(
                    self._content(
                        "user",
                        "Prior tool evidence (not provider continuation): " + content,
                    )
                )
            else:
                contents.append(self._content("user", content))
        contents.append(self._content("user", prompt))
        return contents, system_parts

    def _content(self, role: str, text: str) -> Any:
        return {"role": role, "parts": [{"text": text}]}

    @staticmethod
    def _interaction_tool(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": strip_internal_schema_extensions(tool.input_schema),
        }

    def _generate_content_tools(self, tools: list[ToolDefinition]) -> list[Any]:
        return [
            {
                "function_declarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters_json_schema": strip_internal_schema_extensions(
                            tool.input_schema
                        ),
                    }
                ]
            }
            for tool in tools
        ]

    @staticmethod
    def _dump_content(content: Any) -> dict[str, Any]:
        return _provider_mapping(content)

    def _parse_interaction(
        self,
        interaction: Any,
        available_tools: list[ToolDefinition],
        *,
        model: str,
        input_steps: list[dict[str, Any]],
        effective_parameters: Mapping[str, Any],
    ) -> LLMResponse:
        status_value = _value(interaction, "status")
        status_text = getattr(status_value, "value", status_value)
        status = (
            str(status_text).strip().lower()
            if status_text is not None and str(status_text).strip()
            else None
        )
        provider_errors = _provider_error_evidence(_value(interaction, "errors"))
        actionable_statuses = {"completed", "requires_action"}
        if (
            status is not None and status not in actionable_statuses
        ) or provider_errors:
            raise StructuredToolTransportError(
                "Gemini Interactions returned an unsuccessful provider status.",
                decision={
                    "provider": "gemini",
                    "effective_api_surface": GEMINI_INTERACTIONS_SURFACE,
                    "model": model,
                    "provider_status": status,
                    "provider_errors": provider_errors,
                    "failure_kind": "provider_response_failed",
                },
            )
        output_steps = [
            _provider_mapping(step) for step in (_value(interaction, "steps") or [])
        ]
        output_steps = [step for step in output_steps if step]
        text = _value(interaction, "output_text")
        if not isinstance(text, str):
            text = self._text_from_interaction_steps(output_steps)
        tool_calls, diagnostics, call_names = self._normalise_function_steps(
            output_steps,
            available_tools,
            surface=GEMINI_INTERACTIONS_SURFACE,
        )
        if status == "requires_action" and not tool_calls:
            raise StructuredToolProtocolError(
                "Gemini Interactions required action without a correlated function call.",
                decision={
                    "provider": "gemini",
                    "effective_api_surface": GEMINI_INTERACTIONS_SURFACE,
                    "provider_status": status,
                    "failure_kind": "missing_provider_call_correlation",
                },
            )
        actual_model = _value(interaction, "model")
        return self._response(
            text=text or "",
            tool_calls=tool_calls,
            diagnostics=diagnostics,
            provider_call_names=call_names,
            output_items=output_steps,
            input_items=input_steps,
            requested_model=model,
            actual_model=actual_model,
            surface=GEMINI_INTERACTIONS_SURFACE,
            usage=_value(interaction, "usage"),
            raw_response={
                "id": _value(interaction, "id"),
                "model": actual_model,
                "status": _value(interaction, "status"),
                "steps": output_steps,
                "usage": _provider_mapping(_value(interaction, "usage")),
                "errors": provider_errors,
            },
            effective_parameters=effective_parameters,
        )

    def _parse_generate_content(
        self,
        response: Any,
        available_tools: list[ToolDefinition],
        *,
        model: str,
        input_items: list[dict[str, Any]],
        effective_parameters: Mapping[str, Any],
    ) -> LLMResponse:
        candidates = _value(response, "candidates") or []
        content = _value(candidates[0], "content") if candidates else None
        output_items = [self._dump_content(content)] if content is not None else []
        function_steps: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for part in _value(content, "parts") or []:
            part_text = _value(part, "text")
            if isinstance(part_text, str) and not bool(_value(part, "thought")):
                text_parts.append(part_text)
            function_call = _value(part, "function_call")
            if function_call is not None:
                function_steps.append(
                    {
                        "type": "function_call",
                        "id": _value(function_call, "id"),
                        "name": _value(function_call, "name"),
                        "arguments": _value(function_call, "args"),
                    }
                )
        if not function_steps:
            provider_calls = _value(response, "function_calls") or []
            for function_call in provider_calls:
                function_steps.append(
                    {
                        "type": "function_call",
                        "id": _value(function_call, "id"),
                        "name": _value(function_call, "name"),
                        "arguments": _value(function_call, "args"),
                    }
                )
        tool_calls, diagnostics, call_names = self._normalise_function_steps(
            function_steps,
            available_tools,
            surface=GEMINI_GENERATE_CONTENT_SURFACE,
        )
        actual_model = _value(response, "model_version") or model
        raw_candidates: list[dict[str, Any]] = []
        for candidate in candidates[:1]:
            raw_candidates.append(
                {
                    "content": self._dump_content(_value(candidate, "content")),
                    "finish_reason": str(_value(candidate, "finish_reason") or ""),
                }
            )
        self._apply_generate_content_call_ids(output_items, function_steps)
        return self._response(
            text=(
                "".join(text_parts)
                or (
                    _value(response, "text")
                    if isinstance(_value(response, "text"), str)
                    else ""
                )
            ),
            tool_calls=tool_calls,
            diagnostics=diagnostics,
            provider_call_names=call_names,
            output_items=output_items,
            input_items=input_items,
            requested_model=model,
            actual_model=actual_model,
            surface=GEMINI_GENERATE_CONTENT_SURFACE,
            usage=_value(response, "usage_metadata"),
            raw_response={
                "response_id": _value(response, "response_id"),
                "model_version": actual_model,
                "candidates": raw_candidates,
                "usage_metadata": _provider_mapping(_value(response, "usage_metadata")),
            },
            effective_parameters=effective_parameters,
        )

    def _normalise_function_steps(
        self,
        steps: Sequence[Mapping[str, Any]],
        available_tools: list[ToolDefinition],
        *,
        surface: str,
    ) -> tuple[list[ToolCall], list[dict[str, Any]], dict[str, str]]:
        known = {tool.name: tool for tool in available_tools}
        calls: list[ToolCall] = []
        diagnostics: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}
        seen: set[str] = set()
        for step in steps:
            if step.get("type") != "function_call":
                continue
            call_id = str(step.get("id") or "").strip()
            name = str(step.get("name") or "").strip()
            provider_item_id = call_id or None
            if not name or (
                not call_id and surface == GEMINI_INTERACTIONS_SURFACE
            ):
                raise StructuredToolProtocolError(
                    "Gemini returned a function call without the required id and name.",
                    decision={
                        "provider": "gemini",
                        "effective_api_surface": surface,
                        "failure_kind": "missing_provider_call_correlation",
                    },
                )
            if not call_id:
                call_id = f"von-gemini-{uuid4()}"
                if isinstance(step, dict):
                    step["id"] = call_id
            if call_id in seen:
                raise StructuredToolProtocolError(
                    "Gemini returned duplicate provider function-call IDs."
                )
            seen.add(call_id)
            call_names[call_id] = name
            arguments = step.get("arguments")
            if not isinstance(arguments, Mapping):
                diagnostics.append(
                    validation_diagnostic(
                        tool=name,
                        error_code="arguments_not_object",
                        message=f"Tool '{name}' arguments must be a JSON object.",
                        payload={"call_id": call_id},
                    )
                )
                continue
            if name not in known:
                diagnostics.append(
                    validation_diagnostic(
                        tool=name,
                        error_code="unknown_tool",
                        message=f"Unknown tool requested: {name}",
                        payload={"call_id": call_id},
                    )
                )
                continue
            calls.append(
                ToolCall(
                    tool_name=name,
                    payload=dict(arguments),
                    call_id=call_id,
                    provider_item_id=provider_item_id,
                )
            )
        if diagnostics and not calls:
            raise StructuredToolProtocolError(
                "Gemini returned function calls, but none satisfied the advertised "
                "tool contract.",
                decision={
                    "provider": "gemini",
                    "effective_api_surface": surface,
                    "failure_kind": "provider_tool_call_rejected",
                },
            )
        return calls, diagnostics, call_names

    @staticmethod
    def _apply_generate_content_call_ids(
        output_items: list[dict[str, Any]],
        function_steps: Sequence[Mapping[str, Any]],
    ) -> None:
        """Add legacy internal IDs to the exact content retained for replay."""

        call_ids = [str(step.get("id") or "").strip() for step in function_steps]
        call_index = 0
        for content in output_items:
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if call_index >= len(call_ids) or not isinstance(part, dict):
                    continue
                function_call = part.get("function_call")
                if not isinstance(function_call, dict):
                    function_call = part.get("functionCall")
                if not isinstance(function_call, dict):
                    continue
                call_id = call_ids[call_index]
                call_index += 1
                if not str(function_call.get("id") or "").strip() and call_id:
                    function_call["id"] = call_id

    def _response(
        self,
        *,
        text: str,
        tool_calls: list[ToolCall],
        diagnostics: list[dict[str, Any]],
        provider_call_names: Mapping[str, str],
        output_items: list[dict[str, Any]],
        input_items: list[dict[str, Any]],
        requested_model: str,
        actual_model: Any,
        surface: str,
        usage: Any,
        raw_response: dict[str, Any],
        effective_parameters: Mapping[str, Any],
    ) -> LLMResponse:
        accepted_ids = [call.call_id for call in tool_calls]
        rejected: list[dict[str, str]] = []
        for diagnostic in diagnostics:
            payload = diagnostic.get("payload")
            call_id = payload.get("call_id") if isinstance(payload, Mapping) else None
            if isinstance(call_id, str) and call_id.strip():
                rejected.append(
                    {
                        "call_id": call_id,
                        "name": provider_call_names[call_id],
                        "error_code": str(
                            diagnostic.get("error_code") or "invalid_provider_tool_call"
                        ),
                    }
                )
        effective_model = (
            str(actual_model) if isinstance(actual_model, str) else requested_model
        )
        transport = {
            "provider": "gemini",
            "requested_model": requested_model,
            "effective_model": effective_model,
            "effective_api_surface": surface,
            "connection_id": self.config.connection_id or "gemini_developer_api",
            "deployment_id": self.config.deployment_id or requested_model,
            "continuation_mode": "stateless",
            "store": False,
            "effective_model_parameters": dict(effective_parameters),
            "provider_tool_call_diagnostic_codes": [
                str(item.get("error_code") or "invalid_provider_tool_call")
                for item in diagnostics
            ],
        }
        continuation = None
        if tool_calls:
            continuation = LLMContinuation(
                provider="gemini",
                api_surface=surface,
                model=requested_model,
                state_mode="stateless",
                connection_id=transport["connection_id"],
                deployment_id=transport["deployment_id"],
                input_items=[dict(item) for item in input_items],
                output_items=[dict(item) for item in output_items],
                transport_decision={
                    **transport,
                    "accepted_tool_call_ids": accepted_ids,
                    "provider_call_names": dict(provider_call_names),
                    "rejected_tool_calls": rejected,
                },
            )
        return LLMResponse(
            text_response=text,
            tool_calls=tool_calls,
            raw_response=raw_response,
            model=effective_model,
            usage=self._usage_mapping(usage),
            tool_call_diagnostics=diagnostics,
            continuation=continuation,
            transport_metadata=transport,
        )

    @staticmethod
    def _text_from_interaction_steps(steps: Sequence[Mapping[str, Any]]) -> str:
        text_parts: list[str] = []
        for step in steps:
            if step.get("type") != "model_output":
                continue
            content = step.get("content")
            if not isinstance(content, Sequence) or isinstance(
                content, (str, bytes, bytearray)
            ):
                continue
            for part in content:
                if isinstance(part, Mapping) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        return "".join(text_parts)

    @staticmethod
    def _usage_mapping(usage: Any) -> dict[str, Any] | None:
        raw = _provider_mapping(usage)
        if not raw:
            return None

        def first(*keys: str) -> Any:
            for key in keys:
                value = raw.get(key)
                if isinstance(value, (int, float)):
                    return value
            return None

        visible_output_tokens = first(
            "candidates_token_count", "total_output_tokens", "visible_output_tokens"
        )
        thought_tokens = first(
            "thoughts_token_count", "total_thought_tokens", "thought_tokens"
        )
        output_tokens = None
        if isinstance(visible_output_tokens, (int, float)) or isinstance(
            thought_tokens, (int, float)
        ):
            output_tokens = (visible_output_tokens or 0) + (thought_tokens or 0)
        mapped = {
            "input_tokens": first(
                "prompt_token_count", "total_input_tokens", "input_tokens"
            ),
            "output_tokens": output_tokens,
            "visible_output_tokens": visible_output_tokens,
            "total_tokens": first("total_token_count", "total_tokens"),
            "cached_input_tokens": first(
                "cached_content_token_count",
                "total_cached_tokens",
                "cached_tokens",
            ),
            "thought_tokens": thought_tokens,
            "tool_use_tokens": first("total_tool_use_tokens", "tool_use_tokens"),
        }
        if (
            mapped["total_tokens"] is None
            and isinstance(mapped["input_tokens"], (int, float))
            and isinstance(mapped["output_tokens"], (int, float))
        ):
            mapped["total_tokens"] = mapped["input_tokens"] + mapped["output_tokens"]
        return {key: value for key, value in mapped.items() if value is not None}

    @staticmethod
    def _coerce_tool_results(raw: Any) -> list[ToolResult]:
        if raw is None:
            return []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise StructuredToolProtocolError("tool_results must be a sequence")
        results: list[ToolResult] = []
        for item in raw:
            if isinstance(item, ToolResult):
                results.append(item)
            elif isinstance(item, Mapping):
                results.append(
                    ToolResult(
                        call_id=str(item.get("call_id") or ""),
                        tool_name=(
                            str(item.get("tool_name"))
                            if isinstance(item.get("tool_name"), str)
                            else None
                        ),
                        output=item.get("output"),
                        status=str(item.get("status") or "ok"),
                    )
                )
            else:
                raise StructuredToolProtocolError("tool result must be a mapping")
        return results

    def _validate_continuation(
        self,
        continuation: LLMContinuation,
        *,
        model: str,
        surface: str,
        tool_results: list[ToolResult],
    ) -> None:
        expected_connection = self.config.connection_id or "gemini_developer_api"
        expected_deployment = self.config.deployment_id or model
        if (
            continuation.provider != "gemini"
            or continuation.api_surface != surface
            or continuation.model != model
            or continuation.state_mode != "stateless"
            or continuation.connection_id != expected_connection
            or continuation.deployment_id != expected_deployment
        ):
            raise StructuredToolProtocolError(
                "Gemini structured continuation does not match the selected "
                "provider, surface, model, or deployment."
            )
        accepted = continuation.transport_decision.get("accepted_tool_call_ids")
        names = continuation.transport_decision.get("provider_call_names")
        if not isinstance(accepted, list) or not isinstance(names, Mapping):
            raise StructuredToolProtocolError(
                "Gemini structured continuation is missing provider call lineage."
            )
        expected_ids = [
            call_id
            for call_id in accepted
            if isinstance(call_id, str) and call_id.strip()
        ]
        actual_ids = [result.call_id for result in tool_results]
        represented_calls = self._continuation_output_calls(continuation)
        rejected = continuation.transport_decision.get("rejected_tool_calls", [])
        rejected_ids = [
            item.get("call_id")
            for item in rejected
            if isinstance(item, Mapping)
            and isinstance(item.get("call_id"), str)
            and item.get("call_id")
        ]
        lineage_ids = [*expected_ids, *rejected_ids]
        if (
            not expected_ids
            or len(expected_ids) != len(set(expected_ids))
            or len(actual_ids) != len(set(actual_ids))
            or set(expected_ids) != set(actual_ids)
            or len(lineage_ids) != len(set(lineage_ids))
            or set(represented_calls) != set(lineage_ids)
        ):
            raise StructuredToolProtocolError(
                "Gemini continuation tool results do not match provider call IDs."
            )
        for result in tool_results:
            expected_name = names.get(result.call_id)
            if not isinstance(expected_name, str) or not expected_name.strip():
                raise StructuredToolProtocolError(
                    "Gemini continuation is missing a correlated function name."
                )
            if result.tool_name is not None and result.tool_name != expected_name:
                raise StructuredToolProtocolError(
                    "Gemini continuation tool result name does not match its call ID."
                )
        for call_id, name in represented_calls.items():
            if names.get(call_id) != name:
                raise StructuredToolProtocolError(
                    "Gemini continuation function names do not match replayed calls."
                )

    @staticmethod
    def _continuation_output_calls(
        continuation: LLMContinuation,
    ) -> dict[str, str]:
        calls: dict[str, str] = {}
        if continuation.api_surface == GEMINI_INTERACTIONS_SURFACE:
            candidate_steps: Sequence[Mapping[str, Any]] = continuation.output_items
        else:
            flattened: list[Mapping[str, Any]] = []
            for content in continuation.output_items:
                parts = content.get("parts")
                if not isinstance(parts, Sequence) or isinstance(
                    parts, (str, bytes, bytearray)
                ):
                    continue
                for part in parts:
                    if not isinstance(part, Mapping):
                        continue
                    function_call = part.get("function_call")
                    if not isinstance(function_call, Mapping):
                        function_call = part.get("functionCall")
                    if isinstance(function_call, Mapping):
                        flattened.append(
                            {
                                "type": "function_call",
                                "id": function_call.get("id"),
                                "name": function_call.get("name"),
                            }
                        )
            candidate_steps = flattened
        for step in candidate_steps:
            if step.get("type") != "function_call":
                continue
            call_id = step.get("id")
            name = step.get("name")
            if not isinstance(call_id, str) or not call_id.strip():
                raise StructuredToolProtocolError(
                    "Gemini continuation contains an uncorrelated function call."
                )
            if not isinstance(name, str) or not name.strip() or call_id in calls:
                raise StructuredToolProtocolError(
                    "Gemini continuation contains invalid function-call lineage."
                )
            calls[call_id] = name
        return calls

    def _interaction_function_results(
        self,
        continuation: LLMContinuation,
        results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        names = continuation.transport_decision["provider_call_names"]
        items = [
            {
                "type": "function_result",
                "name": names[result.call_id],
                "call_id": result.call_id,
                "result": [{"type": "text", "text": _json_text(result.output)}],
                **({"is_error": True} if result.status.lower() != "ok" else {}),
            }
            for result in results
        ]
        for rejected in continuation.transport_decision.get("rejected_tool_calls", []):
            if not isinstance(rejected, Mapping):
                continue
            items.append(
                {
                    "type": "function_result",
                    "name": rejected["name"],
                    "call_id": rejected["call_id"],
                    "result": [
                        {
                            "type": "text",
                            "text": _json_text(
                                {
                                    "status": "not_executed",
                                    "error_code": rejected["error_code"],
                                }
                            ),
                        }
                    ],
                    "is_error": True,
                }
            )
        return items

    def _content_function_results(
        self,
        continuation: LLMContinuation,
        results: list[ToolResult],
    ) -> list[Any]:
        names = continuation.transport_decision["provider_call_names"]
        parts = []
        for result in results:
            response_key = "output" if result.status.lower() == "ok" else "error"
            parts.append(
                self._genai.types.Part(
                    function_response=self._genai.types.FunctionResponse(
                        id=result.call_id,
                        name=names[result.call_id],
                        response={response_key: result.output},
                    )
                )
            )
        for rejected in continuation.transport_decision.get("rejected_tool_calls", []):
            if not isinstance(rejected, Mapping):
                continue
            parts.append(
                self._genai.types.Part(
                    function_response=self._genai.types.FunctionResponse(
                        id=rejected["call_id"],
                        name=rejected["name"],
                        response={"error": rejected["error_code"]},
                    )
                )
            )
        return [self._genai.types.Content(role="user", parts=parts)] if parts else []
