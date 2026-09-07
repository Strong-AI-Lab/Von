"""OpenAI structured-tool adapter for Chat Completions and Responses."""

from __future__ import annotations

from ....services.conversation_image_service import provider_image_messages

import asyncio
import hashlib
import inspect
import json
import logging
import re
import uuid
from collections.abc import Mapping
from time import monotonic
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

import openai

from ..client import (
    LLMClient,
    LLMClientConfig,
    observe_request_advisory,
    resolve_safe_temperature_for_model,
    split_request_advisory_from_llm_params,
)
from ..transport import (
    API_SURFACE_CHAT_COMPLETIONS,
    API_SURFACE_RESPONSES,
    StructuredToolTransportDecision,
    resolve_structured_tool_transport,
    sanitise_transport_telemetry_text,
    sanitise_transport_telemetry_value,
)
from ..types import (
    LLMContinuation,
    LLMResponse,
    StructuredToolCapabilityRejectedError,
    StructuredToolContextLimitError,
    StructuredToolProtocolError,
    StructuredToolTransportError,
    ToolCall,
    ToolCallError,
    ToolDefinition,
    ToolResult,
    UnsupportedStructuredToolTransportError,
)
from ...model_defaults import (
    META_MUSE_MAX_OUTPUT_TOKENS,
    META_MUSE_TEMPERATURE,
    META_MUSE_TOP_P,
)
from ....integrations.internal_mcp.tool_call_contracts import validation_diagnostic
from ....services.llm_api_key_resolution import (
    classify_api_key_failover_exception,
)
from ....services.model_parameter_service import (
    chat_completions_kwargs_from_model_parameters,
    responses_kwargs_from_model_parameters,
)


logger = logging.getLogger(__name__)

_OPENROUTER_REQUIRED_PROVIDER_PREFERENCES: dict[str, Any] = {
    "zdr": True,
    "data_collection": "deny",
    "require_parameters": True,
}


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _provider_item_mapping(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    if hasattr(item, "model_dump"):
        dumped = item.model_dump(exclude_none=True)
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _raw_reasoning_effort(raw: Any) -> Any:
    if not isinstance(raw, Mapping):
        return None
    payload = raw.get("model_parameters")
    if not isinstance(payload, Mapping):
        payload = raw
    effort = payload.get("reasoning_effort")
    if effort is None:
        effort = payload.get("reasoningEffort")
    nested = payload.get("reasoning")
    if effort is None and isinstance(nested, Mapping):
        effort = nested.get("effort")
    return effort


def _merge_provider_parameter_kwargs(
    target: dict[str, Any],
    source: Mapping[str, Any],
) -> None:
    """Merge provider projections without discarding adjacent nested options."""

    for key, value in source.items():
        current = target.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            target[key] = {**dict(current), **dict(value)}
        else:
            target[key] = value


def _provider_label(provider: str) -> str:
    return {
        "openai": "OpenAI",
        "openrouter": "OpenRouter",
        "meta": "Meta Model API",
    }.get(str(provider or "").strip().lower(), "OpenAI-compatible provider")


class OpenAIClient(LLMClient):
    """OpenAI client whose wire surface is selected from represented profiles."""

    def __init__(self, config: LLMClientConfig):
        super().__init__(config)
        kwargs: Dict[str, Any] = {}
        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client_kwargs = kwargs
        self._backup_client_kwargs = {
            **kwargs,
            **({"api_key": config.backup_api_key} if config.backup_api_key else {}),
        }
        self._client: Any | None = None

    async def aclose(self) -> None:
        """Close a lazily-created native-async client on its owning loop."""

        client = self._client
        self._client = None
        await self._close_request_client(client)

    @staticmethod
    async def _close_request_client(client: Any) -> None:
        if client is None:
            return
        close = getattr(client, "close", None)
        if not callable(close):
            close = getattr(client, "aclose", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def generate_with_tools(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        for tool in available_tools:
            self._validate_input_schema(tool)

        request_kwargs = dict(kwargs)
        request_client_base = request_kwargs.pop("_request_client", None)
        injected_backup_client = request_kwargs.pop("_backup_request_client", None)
        if request_client_base is None:
            if self._client is None:
                self._client = openai.AsyncOpenAI(**self._client_kwargs)
            request_client_base = self._client
        active_request_client = request_client_base
        owned_backup_client: Any | None = None
        credential_source = "primary"
        primary_credential_failure_kind: str | None = None
        request_model = request_kwargs.pop("model", None) or self.config.model
        raw_llm_params = request_kwargs.pop("llm_params", None)
        llm_params, request_advisory_seconds = split_request_advisory_from_llm_params(
            raw_llm_params
        )
        request_started_monotonic = monotonic()
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
        parameter_projection = (
            dict(llm_params or {}) if isinstance(llm_params, Mapping) else {}
        )

        decision = resolve_structured_tool_transport(
            provider=self.config.provider or "openai",
            model=request_model,
            tools_present=bool(available_tools),
            requested_api_surface=(
                continuation.api_surface
                if continuation is not None
                else self.config.requested_api_surface
            ),
            connection_id=self._resolved_connection_id(),
            deployment_id=self.config.deployment_id or request_model,
            parameter_projection=parameter_projection,
            allow_advertised_surface_override=continuation is not None,
        )
        decision_telemetry = decision.to_telemetry()
        provider_label = _provider_label(decision.provider)
        if decision.status != "compatible":
            raise UnsupportedStructuredToolTransportError(
                "No represented API profile supports structured tools for "
                f"provider={decision.provider!r} model={decision.model!r}.",
                decision=decision_telemetry,
            )
        if continuation is not None:
            self._validate_continuation(continuation, decision)

        async def _invoke(
            selected_decision: StructuredToolTransportDecision,
            request_client: Any,
        ) -> LLMResponse:
            selected_request_kwargs = dict(request_kwargs)
            if selected_decision.effective_api_surface == API_SURFACE_RESPONSES:
                response = await self._generate_responses(
                    prompt=prompt,
                    available_tools=available_tools,
                    context=context,
                    system_message=system_message,
                    request_model=request_model,
                    llm_params=llm_params,
                    continuation=continuation,
                    tool_results=tool_results,
                    request_kwargs=selected_request_kwargs,
                    decision=selected_decision,
                    request_client=request_client,
                )
            else:
                response = await self._generate_chat_completions(
                    prompt=prompt,
                    available_tools=available_tools,
                    context=context,
                    system_message=system_message,
                    request_model=request_model,
                    llm_params=llm_params,
                    continuation=continuation,
                    tool_results=tool_results,
                    request_kwargs=selected_request_kwargs,
                    decision=selected_decision,
                    request_client=request_client,
                )
            observe_request_advisory(
                provider=decision.provider,
                advisory_seconds=request_advisory_seconds,
                started_monotonic=request_started_monotonic,
                event_logger=self.logger,
            )
            return response

        async def _invoke_with_credential_failover(
            selected_decision: StructuredToolTransportDecision,
        ) -> LLMResponse:
            nonlocal active_request_client
            nonlocal credential_source
            nonlocal owned_backup_client
            nonlocal primary_credential_failure_kind

            try:
                response = await _invoke(selected_decision, active_request_client)
            except StructuredToolTransportError:
                raise
            except Exception as primary_exc:
                if credential_source != "primary":
                    raise
                failure_kind = classify_api_key_failover_exception(
                    selected_decision.provider,
                    primary_exc,
                )
                if not failure_kind or not self.config.backup_api_key:
                    raise
                primary_credential_failure_kind = failure_kind
                if injected_backup_client is not None:
                    active_request_client = injected_backup_client
                else:
                    owned_backup_client = openai.AsyncOpenAI(
                        **self._backup_client_kwargs
                    )
                    active_request_client = owned_backup_client
                credential_source = "backup"
                self.logger.warning(
                    "%s primary credential rejected (%s); retrying once with backup credential.",
                    provider_label,
                    failure_kind,
                )
                response = await _invoke(selected_decision, active_request_client)

            response.transport_metadata.update(
                {
                    "credential_source": credential_source,
                    "credential_failover_used": credential_source == "backup",
                }
            )
            if primary_credential_failure_kind:
                response.transport_metadata["primary_credential_failure_kind"] = (
                    primary_credential_failure_kind
                )
            return response

        try:
            return await _invoke_with_credential_failover(decision)
        except StructuredToolTransportError:
            raise
        except Exception as exc:
            safe_error = self._sanitise_provider_error(exc)
            if decision.provider == "meta":
                for secret in (self.config.api_key, self.config.backup_api_key):
                    if secret:
                        safe_error = safe_error.replace(secret, "[redacted]")
            self.logger.error(
                "%s structured-tool API error: %s", provider_label, safe_error
            )
            if self._has_exact_context_length_exceeded_code(exc):
                raise StructuredToolContextLimitError(
                    f"{provider_label} rejected the structured-tool request because its "
                    f"context exceeded the model limit: {safe_error}",
                    decision={
                        **decision.to_telemetry(),
                        **self._provider_error_metadata(exc),
                        "failure_kind": "provider_context_length_exceeded",
                    },
                ) from exc
            if self._looks_like_tool_call_lineage_rejection(exc):
                raise StructuredToolProtocolError(
                    f"{provider_label} rejected a function-call output whose provider call "
                    "lineage was unavailable; refusing an uncorrelated retry: "
                    f"{safe_error}",
                    decision={
                        **decision.to_telemetry(),
                        **self._provider_error_metadata(exc),
                        **self._fresh_tool_context_projection_metadata(
                            context=context,
                            continuation=continuation,
                        ),
                        "failure_kind": "provider_tool_call_lineage_rejected",
                    },
                ) from exc
            if self._looks_like_capability_rejection(exc):
                if continuation is not None:
                    # A provider continuation is surface-specific.  Switching
                    # Responses to Chat (or vice versa) here would silently
                    # discard its reasoning/function-call state and could
                    # manufacture a plausible but ungrounded fresh answer.
                    continuation_rejection = {
                        **decision.to_telemetry(),
                        **self._provider_error_metadata(exc),
                        "surface_fallback_used": False,
                        "surface_fallback_blocked_reason": (
                            "provider_continuation_surface_is_pinned"
                        ),
                    }
                    raise StructuredToolCapabilityRejectedError(
                        f"{provider_label} rejected the represented continuation surface; "
                        "cross-surface fallback is not valid mid-continuation: "
                        f"{safe_error}",
                        decision=continuation_rejection,
                    ) from exc
                alternate_surface = next(
                    (
                        surface
                        for surface in decision.advertised_alternatives
                        if surface
                        in {API_SURFACE_CHAT_COMPLETIONS, API_SURFACE_RESPONSES}
                        and surface != decision.effective_api_surface
                    ),
                    None,
                )
                if alternate_surface is not None:
                    alternate_decision = resolve_structured_tool_transport(
                        provider=decision.provider,
                        model=decision.model,
                        tools_present=decision.tools_present,
                        requested_api_surface=alternate_surface,
                        connection_id=decision.connection_id,
                        deployment_id=decision.deployment_id,
                        parameter_projection=decision.parameter_projection,
                        allow_advertised_surface_override=True,
                    )
                    if (
                        alternate_decision.status != "compatible"
                        or alternate_decision.effective_api_surface != alternate_surface
                    ):
                        alternate_surface = None
                if alternate_surface is not None:
                    try:
                        alternate_response = await _invoke_with_credential_failover(
                            alternate_decision
                        )
                    except StructuredToolTransportError:
                        raise
                    except Exception as alternate_exc:
                        alternate_safe_error = self._sanitise_provider_error(
                            alternate_exc
                        )
                        if self._has_exact_context_length_exceeded_code(alternate_exc):
                            raise StructuredToolContextLimitError(
                                f"{provider_label} rejected the advertised alternate "
                                "structured-tool surface because its context "
                                f"exceeded the model limit: {alternate_safe_error}",
                                decision={
                                    **alternate_decision.to_telemetry(),
                                    **self._provider_error_metadata(alternate_exc),
                                    "surface_fallback_used": True,
                                    "initial_effective_api_surface": (
                                        decision.effective_api_surface
                                    ),
                                    "failure_kind": (
                                        "provider_context_length_exceeded"
                                    ),
                                },
                            ) from alternate_exc
                        if self._looks_like_tool_call_lineage_rejection(alternate_exc):
                            raise StructuredToolProtocolError(
                                f"{provider_label} rejected a function-call output whose "
                                "provider call lineage was unavailable on the "
                                "advertised alternate surface; refusing an "
                                "uncorrelated retry: "
                                f"{alternate_safe_error}",
                                decision={
                                    **alternate_decision.to_telemetry(),
                                    **self._provider_error_metadata(alternate_exc),
                                    **self._fresh_tool_context_projection_metadata(
                                        context=context,
                                        continuation=continuation,
                                    ),
                                    "surface_fallback_used": True,
                                    "initial_effective_api_surface": (
                                        decision.effective_api_surface
                                    ),
                                    "failure_kind": (
                                        "provider_tool_call_lineage_rejected"
                                    ),
                                },
                            ) from alternate_exc
                        if self._looks_like_capability_rejection(alternate_exc):
                            rejection_telemetry = {
                                **alternate_decision.to_telemetry(),
                                **self._provider_error_metadata(alternate_exc),
                                "surface_fallback_used": True,
                                "initial_effective_api_surface": (
                                    decision.effective_api_surface
                                ),
                                "advertised_surface_attempts": [
                                    decision.effective_api_surface,
                                    alternate_surface,
                                ],
                            }
                            raise StructuredToolCapabilityRejectedError(
                                f"{provider_label} rejected structured tools on both represented "
                                f"surfaces; final surface {alternate_surface}: "
                                f"{alternate_safe_error}",
                                decision=rejection_telemetry,
                            ) from alternate_exc
                        alternate_failure_telemetry = {
                            **alternate_decision.to_telemetry(),
                            **self._provider_error_metadata(alternate_exc),
                            "surface_fallback_used": True,
                            "initial_effective_api_surface": (
                                decision.effective_api_surface
                            ),
                            "advertised_surface_attempts": [
                                decision.effective_api_surface,
                                alternate_surface,
                            ],
                            "alternate_failure_kind": "provider_error",
                        }
                        # The first attempt established a capability mismatch,
                        # but a timeout/rate-limit/auth failure on the advertised
                        # alternate is still a provider failure.  Preserve its
                        # native exception class so the existing retry/fallback
                        # path can classify it correctly, while attaching only
                        # credential-free transport evidence for telemetry.
                        try:
                            setattr(
                                alternate_exc,
                                "structured_tool_transport_decision",
                                alternate_failure_telemetry,
                            )
                        except Exception:
                            pass
                        raise
                    alternate_response.transport_metadata.update(
                        {
                            "surface_fallback_used": True,
                            "initial_effective_api_surface": (
                                decision.effective_api_surface
                            ),
                            "advertised_surface_attempts": [
                                decision.effective_api_surface,
                                alternate_surface,
                            ],
                        }
                    )
                    return alternate_response
                rejection_telemetry = {
                    **decision_telemetry,
                    **self._provider_error_metadata(exc),
                }
                raise StructuredToolCapabilityRejectedError(
                    f"{provider_label} rejected structured tools on "
                    f"{decision.effective_api_surface}: {safe_error}",
                    decision=rejection_telemetry,
                ) from exc
            raise ToolCallError(f"{provider_label} call failed: {safe_error}") from exc
        finally:
            await self._close_request_client(owned_backup_client)

    def _resolved_connection_id(self) -> str:
        configured = str(self.config.connection_id or "").strip()
        if configured:
            return configured
        base_url = str(self.config.base_url or "").strip()
        if not base_url:
            return "#V#openai_provider"
        parsed = urlparse(base_url)
        host = str(parsed.hostname or "").strip().lower()
        if host in {"api.openai.com", "openai.com"}:
            return "#V#openai_provider"
        origin = f"{parsed.scheme.lower()}://{host}:{parsed.port or ''}"
        digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:20]
        return f"openai_compatible:{digest}"

    async def _generate_chat_completions(
        self,
        *,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]],
        system_message: Optional[str],
        request_model: str,
        llm_params: Any,
        continuation: LLMContinuation | None,
        tool_results: list[ToolResult],
        request_kwargs: dict[str, Any],
        decision: StructuredToolTransportDecision,
        request_client: Any,
    ) -> LLMResponse:
        messages = self._build_chat_messages(
            prompt,
            context,
            system_message,
            continuation=continuation,
            tool_results=tool_results,
        )
        tools = [self._tool_definition_to_dict(tool) for tool in available_tools]
        if decision.provider == "openrouter":
            raw_extra_body = request_kwargs.get("extra_body")
            extra_body = (
                dict(raw_extra_body) if isinstance(raw_extra_body, Mapping) else {}
            )
            raw_provider_preferences = extra_body.get("provider")
            provider_preferences = (
                dict(raw_provider_preferences)
                if isinstance(raw_provider_preferences, Mapping)
                else {}
            )
            provider_preferences.update(_OPENROUTER_REQUIRED_PROVIDER_PREFERENCES)
            extra_body["provider"] = provider_preferences
            request_kwargs["extra_body"] = extra_body
            raw_extra_headers = request_kwargs.get("extra_headers")
            extra_headers = (
                dict(raw_extra_headers)
                if isinstance(raw_extra_headers, Mapping)
                else {}
            )
            extra_headers["X-OpenRouter-Metadata"] = "enabled"
            request_kwargs["extra_headers"] = extra_headers
        requested_parameters = (
            dict(llm_params) if isinstance(llm_params, Mapping) else {}
        )
        effective_parameters: dict[str, Any] = {}
        direct_reasoning_effort = request_kwargs.pop("reasoning_effort", None)
        if (
            _raw_reasoning_effort(llm_params) is None
            and direct_reasoning_effort is not None
        ):
            requested_parameters["reasoning_effort"] = direct_reasoning_effort
            _merge_provider_parameter_kwargs(
                effective_parameters,
                chat_completions_kwargs_from_model_parameters(
                    {"reasoning_effort": direct_reasoning_effort},
                    provider=decision.provider,
                    model=request_model,
                    profile_concept_id=decision.profile_concept_id,
                ),
            )

        if self.config.max_tokens is not None:
            request_kwargs["max_tokens"] = self.config.max_tokens
        if isinstance(llm_params, Mapping) and llm_params:
            _merge_provider_parameter_kwargs(
                effective_parameters,
                chat_completions_kwargs_from_model_parameters(
                    llm_params,
                    provider=decision.provider,
                    model=request_model,
                    profile_concept_id=decision.profile_concept_id,
                ),
            )
        if decision.provider == "meta":
            projected_reasoning = effective_parameters.get("reasoning")
            effective_parameters = (
                {"reasoning": dict(projected_reasoning)}
                if isinstance(projected_reasoning, Mapping)
                else {}
            )
        _merge_provider_parameter_kwargs(request_kwargs, effective_parameters)
        if (
            tools
            and "reasoning_effort" in request_kwargs
            and request_kwargs.get("reasoning_effort") != "none"
        ):
            request_kwargs.pop("reasoning_effort", None)
            effective_parameters.pop("reasoning_effort", None)
            logger.info(
                "Omitting non-zero reasoning_effort for a represented Chat "
                "Completions structured-tool profile."
            )
        if (
            "temperature" not in requested_parameters
            and "temperature" in request_kwargs
        ):
            requested_parameters["temperature"] = request_kwargs["temperature"]
        requested_temperature = requested_parameters.get(
            "temperature",
            request_kwargs.get("temperature", self.config.temperature),
        )
        request_kwargs.pop("temperature", None)
        effective_parameters.pop("temperature", None)
        safe_temperature = resolve_safe_temperature_for_model(
            request_model,
            requested_temperature,
            api_surface=API_SURFACE_CHAT_COMPLETIONS,
            profile_concept_id=decision.profile_concept_id,
        )
        if safe_temperature is not None:
            request_kwargs["temperature"] = safe_temperature
            effective_parameters["temperature"] = safe_temperature

        response = await request_client.chat.completions.create(
            model=request_model,
            messages=provider_image_messages(messages, surface="chat"),  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            **request_kwargs,
        )
        parsed = self._parse_chat_response(
            response,
            available_tools,
            decision=decision,
            input_items=messages,
        )
        parsed.transport_metadata.update(
            self._fresh_tool_context_projection_metadata(
                context=context,
                continuation=continuation,
            )
        )
        if decision.provider == "openrouter":
            parsed.transport_metadata["provider_routing_preferences"] = (
                sanitise_transport_telemetry_value(
                    request_kwargs["extra_body"]["provider"]
                )
            )
            parsed.transport_metadata["router_metadata_requested"] = True
        if continuation is not None:
            retained_count = (
                len(continuation.input_items)
                + len(continuation.output_items)
                + len(tool_results)
                + len(self._continuation_rejected_tool_calls(continuation))
            )
            prompt_item_count = 1 if prompt.strip() else 0
            parsed.transport_metadata["continuation_stage_context_item_count"] = max(
                0,
                len(messages) - retained_count - prompt_item_count,
            )
        self._attach_parameter_telemetry(
            parsed,
            requested=requested_parameters,
            effective=effective_parameters,
        )
        return parsed

    async def _generate_responses(
        self,
        *,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]],
        system_message: Optional[str],
        request_model: str,
        llm_params: Any,
        continuation: LLMContinuation | None,
        tool_results: list[ToolResult],
        request_kwargs: dict[str, Any],
        decision: StructuredToolTransportDecision,
        request_client: Any,
    ) -> LLMResponse:
        tools = [
            self._tool_definition_to_responses_dict(tool) for tool in available_tools
        ]
        requested_parameters = (
            dict(llm_params) if isinstance(llm_params, Mapping) else {}
        )
        effective_parameters: dict[str, Any] = {}
        direct_reasoning_effort = request_kwargs.pop("reasoning_effort", None)
        raw_direct_reasoning = request_kwargs.pop("reasoning", None)
        if isinstance(raw_direct_reasoning, Mapping):
            direct_reasoning = dict(raw_direct_reasoning)
            nested_effort = direct_reasoning.pop("effort", None)
            if direct_reasoning_effort is None:
                direct_reasoning_effort = nested_effort
            if direct_reasoning:
                request_kwargs["reasoning"] = direct_reasoning
        elif raw_direct_reasoning is not None:
            # An invalid raw shape must not bypass represented reasoning policy.
            requested_parameters["reasoning"] = raw_direct_reasoning
        if (
            _raw_reasoning_effort(llm_params) is None
            and direct_reasoning_effort is not None
        ):
            requested_parameters["reasoning_effort"] = direct_reasoning_effort
            _merge_provider_parameter_kwargs(
                effective_parameters,
                responses_kwargs_from_model_parameters(
                    {"reasoning_effort": direct_reasoning_effort},
                    provider=decision.provider,
                    model=request_model,
                    profile_concept_id=decision.profile_concept_id,
                    include_registry=(
                        decision.provider != "meta"
                        or decision.profile_concept_id is not None
                    ),
                ),
            )
        input_items = self._build_responses_input(
            prompt=prompt,
            context=context,
            continuation=continuation,
            tool_results=tool_results,
        )
        if self.config.max_tokens is not None:
            request_kwargs.setdefault("max_output_tokens", self.config.max_tokens)
        if isinstance(llm_params, Mapping) and llm_params:
            _merge_provider_parameter_kwargs(
                effective_parameters,
                responses_kwargs_from_model_parameters(
                    llm_params,
                    provider=decision.provider,
                    model=request_model,
                    profile_concept_id=decision.profile_concept_id,
                    include_registry=(
                        decision.provider != "meta"
                        or decision.profile_concept_id is not None
                    ),
                ),
            )
        _merge_provider_parameter_kwargs(request_kwargs, effective_parameters)
        if (
            "temperature" not in requested_parameters
            and "temperature" in request_kwargs
        ):
            requested_parameters["temperature"] = request_kwargs["temperature"]
        requested_temperature = (
            META_MUSE_TEMPERATURE
            if decision.provider == "meta"
            else requested_parameters.get(
                "temperature",
                request_kwargs.get("temperature", self.config.temperature),
            )
        )
        request_kwargs.pop("temperature", None)
        effective_parameters.pop("temperature", None)
        safe_temperature = (
            META_MUSE_TEMPERATURE
            if decision.provider == "meta"
            else resolve_safe_temperature_for_model(
                request_model,
                requested_temperature,
                api_surface=API_SURFACE_RESPONSES,
                profile_concept_id=decision.profile_concept_id,
            )
        )
        if safe_temperature is not None:
            request_kwargs["temperature"] = safe_temperature
            effective_parameters["temperature"] = safe_temperature
        if decision.provider == "meta":
            request_kwargs["max_output_tokens"] = META_MUSE_MAX_OUTPUT_TOKENS
            request_kwargs.pop("top_p", None)
            request_kwargs["top_p"] = META_MUSE_TOP_P
            effective_parameters["top_p"] = META_MUSE_TOP_P
        request_kwargs["store"] = decision.store
        if decision.continuation_mode == "stateless" and not decision.store:
            requested_include = request_kwargs.get("include")
            include = (
                list(requested_include)
                if isinstance(requested_include, Sequence)
                and not isinstance(requested_include, (str, bytes, bytearray))
                else []
            )
            if "reasoning.encrypted_content" not in include:
                include.append("reasoning.encrypted_content")
            request_kwargs["include"] = include
        tool_choice = request_kwargs.get("tool_choice")
        if isinstance(tool_choice, Mapping):
            request_kwargs["tool_choice"] = self._responses_tool_choice(tool_choice)
        if (
            continuation is not None
            and decision.continuation_mode == "provider_managed"
            and continuation.response_id
        ):
            request_kwargs["previous_response_id"] = continuation.response_id

        if decision.provider == "meta":
            # Meta Muse is exposed through the streaming Responses contract.
            # The final completed response remains the canonical source for
            # tool items, usage, and provider-observed identity.
            request_kwargs["stream"] = True
            request_kwargs["extra_headers"] = {"Accept": "text/event-stream"}

        response = await request_client.responses.create(
            model=request_model,
            input=provider_image_messages(input_items, surface="responses"),  # type: ignore[arg-type]
            instructions=system_message,
            tools=tools,  # type: ignore[arg-type]
            **request_kwargs,
        )
        if decision.provider == "meta":
            response = await self._consume_meta_responses_stream(
                response,
                decision=decision,
            )
        parsed = self._parse_responses_response(
            response,
            available_tools,
            decision=decision,
            input_items=input_items,
        )
        parsed.transport_metadata.update(
            self._fresh_tool_context_projection_metadata(
                context=context,
                continuation=continuation,
            )
        )
        if continuation is not None:
            retained_count = (
                0
                if continuation.state_mode == "provider_managed"
                else len(continuation.input_items) + len(continuation.output_items)
            )
            retained_count += len(tool_results)
            prompt_item_count = 1 if prompt.strip() else 0
            parsed.transport_metadata["continuation_stage_context_item_count"] = max(
                0,
                len(input_items) - retained_count - prompt_item_count,
            )
        self._attach_parameter_telemetry(
            parsed,
            requested=requested_parameters,
            effective=effective_parameters,
        )
        if decision.provider == "meta":
            parsed.transport_metadata["stream"] = True
        return parsed

    @staticmethod
    async def _consume_meta_responses_stream(
        stream: Any,
        *,
        decision: StructuredToolTransportDecision,
    ) -> Any:
        """Return the final response.completed payload from Meta's SDK stream."""

        if not hasattr(stream, "__aiter__"):
            raise StructuredToolProtocolError(
                "Meta Responses did not return an asynchronous event stream.",
                decision={
                    **decision.to_telemetry(),
                    "failure_kind": "meta_stream_missing",
                },
            )
        completed_response: Any = None
        try:
            async for event in stream:
                event_type = str(_value(event, "type") or "").strip()
                if event_type == "response.completed":
                    completed_response = _value(event, "response")
                    continue
                if event_type in {
                    "error",
                    "response.error",
                    "response.failed",
                    "response.incomplete",
                }:
                    raise StructuredToolTransportError(
                        f"Meta Responses stream terminated with {event_type}.",
                        decision={
                            **decision.to_telemetry(),
                            "failure_kind": "meta_stream_provider_failure",
                            "provider_event_type": event_type,
                        },
                    )
        finally:
            close = getattr(stream, "close", None)
            if not callable(close):
                close = getattr(stream, "aclose", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
        if completed_response is None:
            raise StructuredToolProtocolError(
                "Meta Responses stream ended without response.completed.",
                decision={
                    **decision.to_telemetry(),
                    "failure_kind": "meta_stream_incomplete",
                },
            )
        return completed_response

    def generate_with_tools_sync(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        # This adapter is routinely constructed once per synchronous model
        # request. Keep its async HTTP client inside the same one-shot loop and
        # close it before that loop goes away; otherwise httpx may later try to
        # finalise a connection on an already-closed loop.
        async def _run_request() -> LLMResponse:
            request_client = openai.AsyncOpenAI(**self._client_kwargs)
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
                try:
                    await self._close_request_client(request_client)
                except Exception as exc:  # pragma: no cover - defensive cleanup
                    self.logger.warning(
                        "OpenAI structured-tool client cleanup failed: %s",
                        type(exc).__name__,
                    )

        return asyncio.run(_run_request())

    def _build_chat_messages(
        self,
        prompt: str,
        context: Optional[Sequence[Dict[str, Any]]],
        system_message: Optional[str],
        *,
        continuation: LLMContinuation | None = None,
        tool_results: list[ToolResult] | None = None,
    ) -> List[Dict[str, Any]]:
        if continuation is not None:
            correlated_results = list(tool_results or [])
            self._validate_tool_result_correlation(
                continuation,
                correlated_results,
            )
            if continuation.state_mode != "stateless":
                raise StructuredToolProtocolError(
                    "Chat Completions structured continuations must be stateless."
                )
            messages = [
                *[dict(item) for item in continuation.input_items],
                *[dict(item) for item in continuation.output_items],
            ]
            messages.extend(
                self._chat_tool_result_message(result) for result in correlated_results
            )
            messages.extend(
                self._chat_rejected_tool_result_message(rejected)
                for rejected in self._continuation_rejected_tool_calls(continuation)
            )
            seen_message_signatures = {
                signature
                for item in messages
                if (signature := self._chat_message_signature(item)) is not None
            }
            stage_messages: list[Mapping[str, Any]] = []
            if system_message:
                stage_messages.append({"role": "system", "content": system_message})
            stage_messages.extend(context or ())
            for message in stage_messages:
                if not isinstance(message, Mapping) or "content" not in message:
                    continue
                if str(message.get("role") or "").strip().lower() == "tool":
                    # Tool results are emitted once, immediately after the
                    # provider assistant tool-call message above.
                    continue
                projected = self._chat_context_message(message)
                signature = self._chat_message_signature(projected)
                if signature is not None and signature in seen_message_signatures:
                    continue
                messages.append(projected)
                if signature is not None:
                    seen_message_signatures.add(signature)
            if prompt.strip():
                messages.append({"role": "user", "content": prompt})
            return messages

        messages: List[Dict[str, Any]] = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        for msg in context or ():
            if not isinstance(msg, Mapping) or "content" not in msg:
                continue
            messages.append(self._chat_context_message(msg))
        messages.append({"role": "user", "content": prompt})
        return messages

    # Backward-compatible test seam.
    _build_messages = _build_chat_messages

    @staticmethod
    def _chat_context_message(message: Mapping[str, Any]) -> Dict[str, Any]:
        role = str(message.get("role") or "user")
        if role == "model":
            role = "assistant"
        if role == "tool":
            # A fresh model request has no provider continuation containing the
            # matching assistant function-call item.  Re-emitting accumulated
            # tool evidence as a native tool message would create an orphan
            # function output.  Preserve the evidence and its correlation as
            # ordinary, untrusted context instead.
            return OpenAIClient._fresh_tool_context_evidence_message(message)
        if role not in {"system", "developer", "user", "assistant"}:
            role = "user"
        return {
            "role": role,
            "content": str(message.get("content") or ""),
            **(
                {"image_attachments": message["image_attachments"]}
                if message.get("image_attachments")
                else {}
            ),
        }

    @staticmethod
    def _chat_tool_result_message(result: ToolResult) -> dict[str, Any]:
        output = result.output
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=True, default=str)
        message: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": result.call_id,
            "content": output,
        }
        return message

    @staticmethod
    def _chat_rejected_tool_result_message(
        rejected: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": str(rejected["call_id"]),
            "content": json.dumps(
                {
                    "status": "not_executed",
                    "error_code": str(
                        rejected.get("error_code") or "invalid_provider_tool_call"
                    ),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def _chat_message_signature(item: Mapping[str, Any]) -> tuple[str, str] | None:
        role = str(item.get("role") or "").strip().lower()
        content = item.get("content")
        if role and isinstance(content, str):
            return role, content + json.dumps(
                item.get("image_attachments", []), sort_keys=True
            )
        return None

    def _build_responses_input(
        self,
        *,
        prompt: str,
        context: Optional[Sequence[Dict[str, Any]]],
        continuation: LLMContinuation | None,
        tool_results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        if continuation is not None:
            self._validate_tool_result_correlation(continuation, tool_results)
            if continuation.state_mode == "provider_managed":
                items: list[dict[str, Any]] = []
            else:
                items = [
                    *[dict(item) for item in continuation.input_items],
                    *[dict(item) for item in continuation.output_items],
                ]
            items.extend(
                self._responses_tool_result_item(result) for result in tool_results
            )
            items.extend(
                self._responses_rejected_tool_result_item(rejected)
                for rejected in self._continuation_rejected_tool_calls(continuation)
            )
            seen_message_signatures = {
                signature
                for item in items
                if (signature := self._responses_message_signature(item)) is not None
            }
            for message in context or ():
                if not isinstance(message, Mapping):
                    continue
                if str(message.get("role") or "").strip().lower() == "tool":
                    # Correlated tool outputs were projected immediately above;
                    # replaying tool-role history as fresh outputs is invalid.
                    continue
                projected = self._responses_context_message_item(message)
                if projected is None:
                    continue
                signature = self._responses_message_signature(projected)
                if signature is not None and signature in seen_message_signatures:
                    continue
                items.append(projected)
                if signature is not None:
                    seen_message_signatures.add(signature)
            if prompt.strip():
                items.append({"role": "user", "content": prompt})
            return items

        items = []
        for msg in context or ():
            if not isinstance(msg, Mapping) or "content" not in msg:
                continue
            role = str(msg.get("role") or "user")
            if role == "model":
                role = "assistant"
            if role == "tool":
                # `function_call_output` is valid only when the same request
                # replays the matching provider function-call item, or carries
                # its provider-managed response ID.  Fresh subworkflow and
                # critic calls often retain prior tool evidence without that
                # opaque provider state, so project it as ordinary context.
                items.append(self._fresh_tool_context_evidence_message(msg))
                continue
            if role not in {"system", "developer", "user", "assistant"}:
                role = "user"
            items.append(
                {
                    "role": role,
                    "content": str(msg.get("content") or ""),
                    **(
                        {"image_attachments": msg["image_attachments"]}
                        if msg.get("image_attachments")
                        else {}
                    ),
                }
            )
        items.append({"role": "user", "content": prompt})
        return items

    @staticmethod
    def _fresh_tool_context_evidence_message(
        message: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project prior tool evidence without claiming provider continuation.

        The user-role envelope deliberately keeps tool output below the system
        and developer trust boundary.  It retains the original tool name and
        call ID for diagnosis/correlation, but it is not a provider-native
        function-call result and therefore cannot be mistaken for one on a
        fresh structured-tool request.
        """

        evidence: dict[str, Any] = {
            "schema_version": "structured_tool_context_evidence.v1",
            "type": "prior_tool_result",
            "trust_boundary": "untrusted_tool_output",
            "output": str(message.get("content") or ""),
        }
        tool_name = message.get("name")
        if isinstance(tool_name, str) and tool_name.strip():
            evidence["tool_name"] = tool_name.strip()
        call_id = message.get("tool_call_id")
        if isinstance(call_id, str) and call_id.strip():
            evidence["call_id"] = call_id.strip()
        return {
            "role": "user",
            "content": json.dumps(
                evidence,
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def _fresh_tool_context_projection_metadata(
        *,
        context: Optional[Sequence[Dict[str, Any]]],
        continuation: LLMContinuation | None,
    ) -> dict[str, Any]:
        if continuation is not None:
            return {"fresh_tool_context_evidence_projection_count": 0}
        count = sum(
            1
            for message in context or ()
            if isinstance(message, Mapping)
            and str(message.get("role") or "").strip().lower() == "tool"
        )
        return {
            "fresh_tool_context_evidence_projection_count": count,
            **(
                {"fresh_tool_context_evidence_projection": ("user_role_json_envelope")}
                if count
                else {}
            ),
        }

    @staticmethod
    def _responses_context_message_item(
        message: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if "content" not in message:
            return None
        role = str(message.get("role") or "user")
        if role == "model":
            role = "assistant"
        if role not in {"system", "developer", "user", "assistant"}:
            role = "user"
        return {
            "role": role,
            "content": str(message.get("content") or ""),
            **(
                {"image_attachments": message["image_attachments"]}
                if message.get("image_attachments")
                else {}
            ),
        }

    @staticmethod
    def _responses_message_signature(item: Mapping[str, Any]) -> tuple[str, str] | None:
        role = str(item.get("role") or "").strip().lower()
        content = item.get("content")
        if item.get("type") == "message" and isinstance(content, Sequence):
            text_parts = [
                str(_value(part, "text"))
                for part in content
                if _value(part, "type") in {"input_text", "output_text"}
                and isinstance(_value(part, "text"), str)
            ]
            content = "".join(text_parts)
        if role and isinstance(content, str):
            return role, content + json.dumps(
                item.get("image_attachments", []), sort_keys=True
            )
        return None

    def _tool_definition_to_responses_dict(
        self,
        tool: ToolDefinition,
    ) -> Dict[str, Any]:
        chat_tool = self._tool_definition_to_dict(tool)
        function = dict(chat_tool["function"])
        function.setdefault("strict", False)
        return {"type": "function", **function}

    @staticmethod
    def _responses_tool_choice(tool_choice: Mapping[str, Any]) -> Any:
        function = tool_choice.get("function")
        if isinstance(function, Mapping) and isinstance(function.get("name"), str):
            return {"type": "function", "name": function["name"]}
        return dict(tool_choice)

    @staticmethod
    def _responses_tool_result_item(result: ToolResult) -> dict[str, Any]:
        output = result.output
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=True, default=str)
        return {
            "type": "function_call_output",
            "call_id": result.call_id,
            "output": output,
        }

    @staticmethod
    def _responses_rejected_tool_result_item(
        rejected: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return a bounded protocol result for a call rejected before execution."""

        return {
            "type": "function_call_output",
            "call_id": str(rejected["call_id"]),
            "output": json.dumps(
                {
                    "status": "not_executed",
                    "error_code": str(
                        rejected.get("error_code") or "invalid_provider_tool_call"
                    ),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        }

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
                continue
            if not isinstance(item, Mapping):
                raise StructuredToolProtocolError("tool result must be a mapping")
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
        return results

    @staticmethod
    def _validate_continuation(
        continuation: LLMContinuation,
        decision: StructuredToolTransportDecision,
    ) -> None:
        if continuation.provider != decision.provider:
            raise StructuredToolProtocolError(
                "Structured continuation provider does not match selected provider.",
                decision=decision.to_telemetry(),
            )
        if continuation.api_surface != decision.effective_api_surface:
            raise StructuredToolProtocolError(
                "Structured continuation API surface does not match selected surface.",
                decision=decision.to_telemetry(),
            )
        if continuation.model != decision.model:
            raise StructuredToolProtocolError(
                "Structured continuation model does not match selected model.",
                decision=decision.to_telemetry(),
            )
        if continuation.state_mode != decision.continuation_mode:
            raise StructuredToolProtocolError(
                "Structured continuation state mode does not match the represented "
                "continuation profile.",
                decision=decision.to_telemetry(),
            )
        if (
            continuation.state_mode == "provider_managed"
            and not continuation.response_id
        ):
            raise StructuredToolProtocolError(
                "Provider-managed structured continuation is missing response_id.",
                decision=decision.to_telemetry(),
            )
        if continuation.connection_id != decision.connection_id:
            raise StructuredToolProtocolError(
                "Structured continuation connection does not match selected connection.",
                decision=decision.to_telemetry(),
            )
        if continuation.deployment_id != decision.deployment_id:
            raise StructuredToolProtocolError(
                "Structured continuation deployment does not match selected deployment.",
                decision=decision.to_telemetry(),
            )
        accepted_call_ids = OpenAIClient._continuation_accepted_tool_call_ids(
            continuation
        )
        rejected_call_ids = [
            item["call_id"]
            for item in OpenAIClient._continuation_rejected_tool_calls(continuation)
        ]
        represented_call_ids = OpenAIClient._continuation_output_tool_call_ids(
            continuation
        )
        expected_call_ids = [*accepted_call_ids, *rejected_call_ids]
        if (
            not accepted_call_ids
            or len(accepted_call_ids) != len(set(accepted_call_ids))
            or len(expected_call_ids) != len(set(expected_call_ids))
            or len(represented_call_ids) != len(set(represented_call_ids))
            or set(expected_call_ids) != set(represented_call_ids)
        ):
            raise StructuredToolProtocolError(
                "Structured continuation call IDs are not backed by valid "
                "provider tool-call output items.",
                decision=decision.to_telemetry(),
            )

    @staticmethod
    def _continuation_output_tool_call_ids(
        continuation: LLMContinuation,
    ) -> list[str]:
        represented: list[str] = []
        if continuation.api_surface == API_SURFACE_RESPONSES:
            for item in continuation.output_items:
                call_id = item.get("call_id")
                if (
                    item.get("type") == "function_call"
                    and isinstance(call_id, str)
                    and call_id.strip()
                    and isinstance(item.get("name"), str)
                    and str(item.get("name")).strip()
                    and isinstance(item.get("arguments"), str)
                ):
                    represented.append(call_id)
            return represented

        if continuation.api_surface == API_SURFACE_CHAT_COMPLETIONS:
            for item in continuation.output_items:
                if item.get("role") != "assistant":
                    continue
                raw_calls = item.get("tool_calls")
                if not isinstance(raw_calls, Sequence) or isinstance(
                    raw_calls, (str, bytes, bytearray)
                ):
                    continue
                for raw_call in raw_calls:
                    if not isinstance(raw_call, Mapping):
                        continue
                    function = raw_call.get("function")
                    call_id = raw_call.get("id")
                    if (
                        raw_call.get("type") == "function"
                        and isinstance(call_id, str)
                        and call_id.strip()
                        and isinstance(function, Mapping)
                        and isinstance(function.get("name"), str)
                        and str(function.get("name")).strip()
                        and isinstance(function.get("arguments"), str)
                    ):
                        represented.append(call_id)
            return represented

        return represented

    @staticmethod
    def _validate_tool_result_correlation(
        continuation: LLMContinuation,
        tool_results: list[ToolResult],
    ) -> None:
        expected = OpenAIClient._continuation_accepted_tool_call_ids(continuation)
        actual = [result.call_id for result in tool_results]
        if len(expected) != len(set(expected)):
            raise StructuredToolProtocolError(
                "Structured continuation contains duplicate provider call IDs."
            )
        if len(actual) != len(set(actual)):
            raise StructuredToolProtocolError(
                "Structured continuation contains duplicate tool results."
            )
        if set(expected) != set(actual):
            raise StructuredToolProtocolError(
                "Structured continuation tool results do not match provider call IDs."
            )

    @staticmethod
    def _continuation_accepted_tool_call_ids(
        continuation: LLMContinuation,
    ) -> list[str]:
        represented = continuation.transport_decision.get("accepted_tool_call_ids")
        if isinstance(represented, Sequence) and not isinstance(
            represented, (str, bytes, bytearray)
        ):
            return [
                str(call_id)
                for call_id in represented
                if isinstance(call_id, str) and call_id.strip()
            ]
        # Backwards compatibility for continuations created before the
        # accepted/rejected call contract was represented explicitly.
        return [
            str(item.get("call_id"))
            for item in continuation.output_items
            if item.get("type") == "function_call" and item.get("call_id")
        ]

    @staticmethod
    def _continuation_rejected_tool_calls(
        continuation: LLMContinuation,
    ) -> list[dict[str, str]]:
        raw = continuation.transport_decision.get("rejected_tool_calls")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            return []
        accepted = set(OpenAIClient._continuation_accepted_tool_call_ids(continuation))
        rejected: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            call_id = str(item.get("call_id") or "").strip()
            if not call_id or call_id in accepted or call_id in seen:
                continue
            seen.add(call_id)
            rejected.append(
                {
                    "call_id": call_id,
                    "error_code": str(
                        item.get("error_code") or "invalid_provider_tool_call"
                    )[:120],
                }
            )
        return rejected

    def _parse_chat_response(
        self,
        response: Any,
        available_tools: List[ToolDefinition],
        *,
        decision: StructuredToolTransportDecision,
        input_items: list[dict[str, Any]],
    ) -> LLMResponse:
        text_response = ""
        tool_calls: List[ToolCall] = []
        diagnostics: List[Dict[str, Any]] = []
        choices = _value(response, "choices") or []
        provider_tool_call_items: list[dict[str, Any]] = []
        if choices:
            message = _value(choices[0], "message")
            content = _value(message, "content")
            if isinstance(content, str):
                text_response = content
            for provider_call in _value(message, "tool_calls") or []:
                function = _value(provider_call, "function")
                correlation_id = self._normalise_provider_tool_call(
                    tool_name=_value(function, "name"),
                    raw_arguments=_value(function, "arguments"),
                    call_id=_value(provider_call, "id"),
                    provider_item_id=_value(provider_call, "id"),
                    available_tools=available_tools,
                    api_surface=API_SURFACE_CHAT_COMPLETIONS,
                    tool_calls=tool_calls,
                    diagnostics=diagnostics,
                )
                if correlation_id:
                    raw_arguments = _value(function, "arguments")
                    if not isinstance(raw_arguments, str):
                        raw_arguments = json.dumps(
                            raw_arguments,
                            ensure_ascii=True,
                            default=str,
                            separators=(",", ":"),
                        )
                    provider_tool_call_items.append(
                        {
                            "id": correlation_id,
                            "type": "function",
                            "function": {
                                "name": str(_value(function, "name") or ""),
                                "arguments": raw_arguments,
                            },
                        }
                    )
        transport_metadata = decision.to_telemetry()
        if decision.provider == "openrouter":
            generation_id = _value(response, "id")
            if generation_id:
                transport_metadata["generation_id"] = (
                    sanitise_transport_telemetry_value(generation_id)
                )
            router_metadata = _value(response, "openrouter_metadata")
            if router_metadata is not None:
                transport_metadata["openrouter_metadata"] = (
                    sanitise_transport_telemetry_value(
                        _provider_item_mapping(router_metadata) or router_metadata,
                        max_depth=6,
                    )
                )
        service_tier = _value(response, "service_tier")
        if isinstance(service_tier, str) and service_tier.strip():
            transport_metadata["effective_service_tier"] = service_tier.strip().lower()
        accepted_tool_call_ids = [call.call_id for call in tool_calls]
        provider_tool_call_ids = [
            str(item.get("id"))
            for item in provider_tool_call_items
            if isinstance(item.get("id"), str) and str(item.get("id")).strip()
        ]
        transport_metadata["accepted_tool_call_ids"] = (
            sanitise_transport_telemetry_value(
                accepted_tool_call_ids,
                key="provider_call_ids",
            )
        )
        diagnostic_codes = [
            str(item.get("error_code") or "invalid_provider_tool_call")[:120]
            for item in diagnostics
        ]
        transport_metadata["provider_tool_call_diagnostic_codes"] = diagnostic_codes
        rejected_tool_calls: list[dict[str, str]] = []
        seen_rejected_call_ids: set[str] = set()
        for diagnostic in diagnostics:
            payload = diagnostic.get("payload")
            if not isinstance(payload, Mapping):
                continue
            call_id = str(payload.get("call_id") or "").strip()
            if not call_id or call_id in seen_rejected_call_ids:
                continue
            seen_rejected_call_ids.add(call_id)
            rejected_tool_calls.append(
                {
                    "call_id": call_id,
                    "error_code": str(
                        diagnostic.get("error_code") or "invalid_provider_tool_call"
                    )[:120],
                }
            )
        transport_metadata["rejected_tool_calls"] = sanitise_transport_telemetry_value(
            rejected_tool_calls
        )
        if diagnostics and not tool_calls:
            raise StructuredToolProtocolError(
                "Chat Completions returned tool calls, but none satisfied the "
                "advertised tool contract.",
                decision={
                    **transport_metadata,
                    "failure_kind": "chat_provider_tool_call_rejected",
                },
            )
        if len(provider_tool_call_ids) != len(set(provider_tool_call_ids)):
            raise StructuredToolProtocolError(
                "Chat Completions returned duplicate provider call IDs.",
                decision={
                    **transport_metadata,
                    "failure_kind": "duplicate_provider_call_id",
                },
            )
        continuation = None
        if tool_calls:
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": text_response or None,
                "tool_calls": provider_tool_call_items,
            }
            continuation = LLMContinuation(
                provider=decision.provider,
                api_surface=decision.effective_api_surface,
                model=decision.model,
                state_mode=decision.continuation_mode,
                connection_id=decision.connection_id,
                deployment_id=decision.deployment_id,
                input_items=input_items,
                output_items=[assistant_message],
                transport_decision={
                    **transport_metadata,
                    # Exact provider IDs are private continuation protocol state.
                    "accepted_tool_call_ids": list(accepted_tool_call_ids),
                    "rejected_tool_calls": list(rejected_tool_calls),
                },
            )
            # Validate the adapter-created continuation before any caller can
            # dispatch an accepted tool.  Mixed provider output may contain a
            # rejected call whose original item is not replayable (for
            # example, a missing function name); discovering that only on the
            # follow-up request would fail after another call had side effects.
            self._validate_continuation(continuation, decision)
        return LLMResponse(
            text_response=text_response,
            tool_calls=tool_calls,
            raw_response=(
                response.model_dump() if hasattr(response, "model_dump") else None
            ),
            model=_value(response, "model"),
            usage=self._usage_mapping(_value(response, "usage"), responses=False),
            tool_call_diagnostics=diagnostics,
            continuation=continuation,
            transport_metadata=transport_metadata,
        )

    def _parse_responses_response(
        self,
        response: Any,
        available_tools: List[ToolDefinition],
        *,
        decision: StructuredToolTransportDecision,
        input_items: list[dict[str, Any]],
    ) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: List[ToolCall] = []
        diagnostics: List[Dict[str, Any]] = []
        output_items: list[dict[str, Any]] = []
        output_item_types: list[str] = []
        provider_function_call_ids: list[str] = []
        for item in _value(response, "output") or []:
            item_type = str(_value(item, "type") or "unknown")
            output_item_types.append(item_type)
            dumped_item = _provider_item_mapping(item)
            if dumped_item:
                output_items.append(dumped_item)
            if item_type == "message":
                for part in _value(item, "content") or []:
                    if _value(part, "type") == "output_text":
                        text = _value(part, "text")
                        if isinstance(text, str):
                            text_parts.append(text)
                continue
            if item_type != "function_call":
                continue
            raw_call_id = _value(item, "call_id")
            if isinstance(raw_call_id, str) and raw_call_id.strip():
                provider_function_call_ids.append(raw_call_id)
            self._normalise_provider_tool_call(
                tool_name=_value(item, "name"),
                raw_arguments=_value(item, "arguments"),
                call_id=_value(item, "call_id"),
                provider_item_id=_value(item, "id"),
                available_tools=available_tools,
                api_surface=API_SURFACE_RESPONSES,
                tool_calls=tool_calls,
                diagnostics=diagnostics,
            )

        raw_response_id = _value(response, "id")
        accepted_tool_call_ids = [call.call_id for call in tool_calls]
        transport_metadata = decision.to_telemetry()
        service_tier = _value(response, "service_tier")
        if isinstance(service_tier, str) and service_tier.strip():
            transport_metadata["effective_service_tier"] = service_tier.strip().lower()
        transport_metadata["response_id"] = sanitise_transport_telemetry_value(
            raw_response_id,
            key="provider_response_id",
        )
        transport_metadata["response_output_item_types"] = (
            sanitise_transport_telemetry_value(
                output_item_types,
                key="provider_output_item_types",
            )
        )
        transport_metadata["response_tool_call_ids"] = (
            sanitise_transport_telemetry_value(
                accepted_tool_call_ids,
                key="provider_call_ids",
            )
        )
        transport_metadata["accepted_tool_call_ids"] = (
            sanitise_transport_telemetry_value(
                accepted_tool_call_ids,
                key="provider_call_ids",
            )
        )
        rejected_tool_calls: list[dict[str, str]] = []
        seen_rejected_call_ids: set[str] = set()
        for diagnostic in diagnostics:
            payload = diagnostic.get("payload")
            if not isinstance(payload, Mapping):
                continue
            call_id = str(payload.get("call_id") or "").strip()
            if not call_id or call_id in seen_rejected_call_ids:
                continue
            seen_rejected_call_ids.add(call_id)
            rejected_tool_calls.append(
                {
                    "call_id": call_id,
                    "error_code": str(
                        diagnostic.get("error_code") or "invalid_provider_tool_call"
                    )[:120],
                }
            )
        transport_metadata["rejected_tool_calls"] = sanitise_transport_telemetry_value(
            rejected_tool_calls
        )
        diagnostic_codes = [
            str(item.get("error_code") or "invalid_provider_tool_call")[:120]
            for item in diagnostics
        ]
        transport_metadata["provider_tool_call_diagnostic_codes"] = diagnostic_codes
        if (
            tool_calls
            and decision.continuation_mode == "provider_managed"
            and not raw_response_id
        ):
            raise StructuredToolProtocolError(
                "Provider-managed Responses tool call is missing response.id; "
                "refusing tool execution without resumable provider state.",
                decision={
                    **transport_metadata,
                    "failure_kind": "missing_provider_response_id",
                },
            )
        if "missing_provider_call_id" in diagnostic_codes:
            raise StructuredToolProtocolError(
                "Responses returned a function call without the required call_id.",
                decision={
                    **transport_metadata,
                    "failure_kind": "missing_provider_call_id",
                },
            )
        if len(provider_function_call_ids) != len(set(provider_function_call_ids)):
            raise StructuredToolProtocolError(
                "Responses returned duplicate provider call IDs.",
                decision={
                    **transport_metadata,
                    "failure_kind": "duplicate_provider_call_id",
                },
            )
        if "function_call" in output_item_types and not tool_calls:
            raise StructuredToolProtocolError(
                "Responses returned function calls, but none satisfied the "
                "advertised tool contract.",
                decision={
                    **transport_metadata,
                    "failure_kind": "all_provider_tool_calls_rejected",
                },
            )
        continuation = None
        if tool_calls:
            continuation = LLMContinuation(
                provider=decision.provider,
                api_surface=decision.effective_api_surface,
                model=decision.model,
                state_mode=decision.continuation_mode,
                response_id=(str(raw_response_id) if raw_response_id else None),
                connection_id=decision.connection_id,
                deployment_id=decision.deployment_id,
                input_items=input_items,
                output_items=output_items,
                transport_decision={
                    **transport_metadata,
                    # These exact IDs are private continuation protocol state,
                    # not general telemetry.  They are stored only inside the
                    # opaque continuation payload used for correlation.
                    "accepted_tool_call_ids": list(accepted_tool_call_ids),
                    "rejected_tool_calls": list(rejected_tool_calls),
                },
            )
            # Fail before tool dispatch when provider output cannot form an
            # exact, replayable continuation.  This keeps mixed malformed
            # output from becoming a post-side-effect protocol failure.
            self._validate_continuation(continuation, decision)
        return LLMResponse(
            text_response="".join(text_parts),
            tool_calls=tool_calls,
            raw_response=(
                response.model_dump() if hasattr(response, "model_dump") else None
            ),
            model=_value(response, "model"),
            usage=self._usage_mapping(_value(response, "usage"), responses=True),
            tool_call_diagnostics=diagnostics,
            continuation=continuation,
            transport_metadata=transport_metadata,
        )

    # Backward-compatible parser seam used by existing Chat tests.
    def _parse_response(
        self,
        response: Any,
        available_tools: List[ToolDefinition],
    ) -> LLMResponse:
        decision = resolve_structured_tool_transport(
            provider="openai",
            model=str(_value(response, "model") or self.config.model),
            tools_present=bool(available_tools),
        )
        return self._parse_chat_response(
            response,
            available_tools,
            decision=decision,
            input_items=[],
        )

    def _normalise_provider_tool_call(
        self,
        *,
        tool_name: Any,
        raw_arguments: Any,
        call_id: Any,
        provider_item_id: Any,
        available_tools: List[ToolDefinition],
        api_surface: str,
        tool_calls: List[ToolCall],
        diagnostics: List[Dict[str, Any]],
    ) -> str | None:
        name = str(tool_name or "").strip()
        if api_surface == API_SURFACE_RESPONSES and not str(call_id or "").strip():
            diagnostics.append(
                validation_diagnostic(
                    tool=name or None,
                    error_code="missing_provider_call_id",
                    message="Responses function call is missing its call_id.",
                    payload={"api_surface": api_surface},
                )
            )
            return None
        correlation_id = str(call_id or "").strip() or str(uuid.uuid4())
        available_names = {tool.name for tool in available_tools}
        try:
            payload = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str)
                else raw_arguments
            )
        except json.JSONDecodeError as exc:
            diagnostics.append(
                validation_diagnostic(
                    tool=name or None,
                    error_code="provider_tool_call_parse_error",
                    message=f"Failed to parse tool call arguments: {exc}",
                    payload={"call_id": correlation_id, "api_surface": api_surface},
                )
            )
            return correlation_id
        if name not in available_names:
            diagnostics.append(
                validation_diagnostic(
                    tool=name or None,
                    error_code="unknown_tool",
                    message=f"Unknown tool requested: {name}",
                    payload={"call_id": correlation_id, "api_surface": api_surface},
                )
            )
            return correlation_id
        if not isinstance(payload, dict):
            diagnostics.append(
                validation_diagnostic(
                    tool=name,
                    error_code="arguments_not_object",
                    message=f"Tool '{name}' arguments must decode to a JSON object.",
                    payload={"call_id": correlation_id, "api_surface": api_surface},
                )
            )
            return correlation_id
        tool_calls.append(
            ToolCall(
                tool_name=name,
                payload=payload,
                call_id=correlation_id,
                provider_item_id=(str(provider_item_id) if provider_item_id else None),
            )
        )
        return correlation_id

    @staticmethod
    def _usage_mapping(usage: Any, *, responses: bool) -> Optional[Dict[str, Any]]:
        if usage is None:
            return None
        if responses:
            prompt = _value(usage, "input_tokens")
            completion = _value(usage, "output_tokens")
            input_details = _value(usage, "input_tokens_details")
        else:
            prompt = _value(usage, "prompt_tokens")
            completion = _value(usage, "completion_tokens")
            input_details = _value(usage, "prompt_tokens_details")
        usage_payload = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": _value(usage, "total_tokens"),
        }
        cached_input_tokens = _value(input_details, "cached_tokens")
        if cached_input_tokens is not None:
            usage_payload["cached_input_tokens"] = cached_input_tokens
        cache_write_input_tokens = _value(input_details, "cache_write_tokens")
        if cache_write_input_tokens is not None:
            usage_payload["cache_write_input_tokens"] = cache_write_input_tokens
        provider_cost = _value(usage, "cost")
        if provider_cost is not None:
            usage_payload["provider_reported_cost"] = provider_cost
        cost_details = _value(usage, "cost_details")
        if cost_details is not None:
            usage_payload["provider_reported_cost_details"] = (
                sanitise_transport_telemetry_value(
                    _provider_item_mapping(cost_details) or cost_details
                )
            )
        return usage_payload

    @staticmethod
    def _attach_parameter_telemetry(
        response: LLMResponse,
        *,
        requested: Mapping[str, Any],
        effective: Mapping[str, Any],
    ) -> None:
        projected_paths: dict[str, str] = {}
        for key in requested:
            if key in effective:
                projected_paths[key] = key
            elif (
                key == "reasoning_effort"
                and isinstance(effective.get("reasoning"), Mapping)
                and "effort" in effective["reasoning"]
            ):
                projected_paths[key] = "reasoning.effort"
        response.transport_metadata["requested_model_parameters"] = (
            sanitise_transport_telemetry_value(dict(requested))
        )
        response.transport_metadata["effective_provider_parameters"] = (
            sanitise_transport_telemetry_value(dict(effective))
        )
        response.transport_metadata["projected_model_parameter_paths"] = projected_paths
        response.transport_metadata["omitted_model_parameter_names"] = sorted(
            key for key in requested if key not in projected_paths
        )

    @staticmethod
    def _looks_like_tool_call_lineage_rejection(exc: Exception) -> bool:
        """Return whether OpenAI rejected an uncorrelated function output."""

        status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            try:
                if int(status_code) not in {400, 409, 422}:
                    return False
            except (TypeError, ValueError):
                return False
        text = str(exc).lower()
        return "no tool call found for function call output" in text or (
            "function_call_output" in text
            and "call_id" in text
            and any(marker in text for marker in ("not found", "missing", "unknown"))
        )

    @staticmethod
    def _has_exact_context_length_exceeded_code(exc: Exception) -> bool:
        """Use only the provider's structured code to identify context overflow."""

        metadata = OpenAIClient._provider_error_metadata(exc)
        code = metadata.get("provider_error_code")
        return (
            isinstance(code, str) and code.strip().lower() == "context_length_exceeded"
        )

    @staticmethod
    def _looks_like_capability_rejection(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        text = str(exc).lower()
        if status_code is not None and int(status_code) not in {400, 404, 409, 422}:
            return False
        return (
            "function tools" in text
            or "tool" in text
            and "not supported" in text
            or "use /v1/responses" in text
            or "chat/completions" in text
            and "reasoning" in text
        )

    @staticmethod
    def _sanitise_provider_error(exc: Exception) -> str:
        text = sanitise_transport_telemetry_text(str(exc))
        text = re.sub(
            r"(https?://)([^/@\s]+@)?([^/?#\s]+)(?:[^\s]*)",
            lambda match: f"{match.group(1)}{match.group(3)}",
            text,
        )
        return text[:4_000]

    @staticmethod
    def _provider_error_metadata(exc: Exception) -> dict[str, Any]:
        metadata: dict[str, Any] = {"provider_error_class": type(exc).__name__}
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            metadata["provider_status_code"] = status_code
        body = getattr(exc, "body", None)
        nested_error_body = body.get("error") if isinstance(body, Mapping) else None
        error_body = (
            nested_error_body
            if isinstance(nested_error_body, Mapping)
            else body
            if isinstance(body, Mapping)
            else None
        )
        for key in ("code", "param", "type"):
            value = getattr(exc, key, None)
            if value is None and isinstance(error_body, Mapping):
                value = error_body.get(key)
            if isinstance(value, (str, int, float, bool)):
                metadata[f"provider_error_{key}"] = sanitise_transport_telemetry_value(
                    value,
                    key=f"provider_error_{key}",
                )
        return metadata
