"""Core gateway primitives for the internal MCP integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, MutableMapping, Optional

from .schemas import (
    Schema,
    SchemaValidationError,
    coerce_payload_types,
    validate_payload,
)
from .transport import InternalMCPTransport, TransportResult

logger = logging.getLogger(__name__)

_LOG_TAG = "[mcp_gateway]"


class GatewayDisabledError(RuntimeError):
    """Raised when the gateway is invoked while disabled."""


@dataclass(frozen=True)
class MethodDefinition:
    """Describe an internal MCP method surface."""

    name: str
    handler: Callable[..., Any]
    input_schema: Schema
    output_schema: Schema | None = None
    category: str = "read"
    timeout_sec: float | None = None
    description: str | None = None
    write_guardrail: Mapping[str, Any] | None = None

    def resolved_timeout(self, transport: InternalMCPTransport) -> float | None:
        if self.timeout_sec is not None:
            return self.timeout_sec
        if self.category == "write":
            return transport.write_timeout_sec
        if self.category == "read":
            return transport.read_timeout_sec
        return transport.read_timeout_sec


@dataclass
class MethodMetrics:
    calls: int = 0
    failures: int = 0
    last_error: str | None = None
    last_duration_ms: float | None = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_duration_ms": self.last_duration_ms,
        }


class MethodCatalogue:
    """Registry of method definitions."""

    def __init__(self) -> None:
        self._definitions: Dict[str, MethodDefinition] = {}

    def register(self, definition: MethodDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"Method '{definition.name}' already registered.")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> MethodDefinition:
        if name not in self._definitions:
            raise KeyError(f"Method '{name}' not registered.")
        return self._definitions[name]

    def list_methods(self) -> list[str]:
        return sorted(self._definitions.keys())

    @staticmethod
    def _summarise_schema(schema: Schema | None) -> Dict[str, Any] | None:
        if schema is None:
            return None
        return {
            "required": sorted(schema.required.keys()),
            "optional": sorted(schema.optional.keys()),
            "allow_unknown": schema.allow_unknown,
            "description": schema.description,
        }

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {
            name: {
                "category": definition.category,
                "timeout_sec": definition.timeout_sec,
                "has_output_schema": definition.output_schema is not None,
                "description": definition.description,
                "write_guardrail": (
                    dict(definition.write_guardrail)
                    if isinstance(definition.write_guardrail, Mapping)
                    else None
                ),
                "input_schema": self._summarise_schema(definition.input_schema),
                "output_schema": self._summarise_schema(definition.output_schema),
            }
            for name, definition in self._definitions.items()
        }


class InternalMCPGateway:
    """Lightweight gateway that executes registered handlers."""

    def __init__(
        self,
        *,
        catalogue: MethodCatalogue,
        transport: InternalMCPTransport,
        enabled: bool = False,
        log_tag: str = _LOG_TAG,
    ) -> None:
        self._catalogue = catalogue
        self._transport = transport
        self._enabled = bool(enabled)
        self._log_tag = log_tag
        self._total_calls = 0
        self._total_failures = 0
        self._method_metrics: Dict[str, MethodMetrics] = {
            name: MethodMetrics() for name in catalogue.list_methods()
        }

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        if not self._enabled:
            logger.info("%s enabling gateway", self._log_tag)
            self._enabled = True

    def disable(self) -> None:
        if self._enabled:
            logger.info("%s disabling gateway", self._log_tag)
            self._enabled = False

    def register_metrics_if_missing(self, method_name: str) -> None:
        if method_name not in self._method_metrics:
            self._method_metrics[method_name] = MethodMetrics()

    def invoke(
        self, method_name: str, payload: Optional[MutableMapping[str, Any]] = None
    ) -> TransportResult:
        if not self._enabled:
            raise GatewayDisabledError("Internal MCP gateway is disabled.")

        payload_dict: MutableMapping[str, Any] = dict(payload or {})
        definition = self._catalogue.get(method_name)
        self.register_metrics_if_missing(method_name)

        payload_dict, coercion_warnings = coerce_payload_types(
            definition.input_schema,
            payload_dict,
        )
        if coercion_warnings:
            logger.debug(
                "%s coerced payload for %s: %s",
                self._log_tag,
                method_name,
                "; ".join(coercion_warnings),
            )

        ok, errors = validate_payload(definition.input_schema, payload_dict)
        if not ok:
            error_message = "; ".join(errors)
            self._record_failure(method_name, error_message)
            raise SchemaValidationError(error_message)

        timeout = definition.resolved_timeout(self._transport)
        try:
            transport_result = self._transport.execute(
                method_name=definition.name,
                handler=definition.handler,
                payload=dict(payload_dict),
                timeout_sec=timeout,
                log_tag=self._log_tag,
            )
        except Exception as exc:
            message = str(exc)
            logger.exception(
                "%s handler for %s raised: %s", self._log_tag, method_name, message
            )
            self._record_failure(method_name, message)
            raise

        result_payload = transport_result.payload
        if definition.output_schema is not None:
            if not isinstance(result_payload, MutableMapping):
                message = (
                    "Output schema provided but handler returned non-mapping payload."
                )
                self._record_failure(method_name, message)
                raise SchemaValidationError(message)
            # Skip output schema validation for error responses (MCPErrorResponse)
            # Error responses follow a different standardised schema with success=False
            is_error_response = result_payload.get("success") is False
            if not is_error_response:
                ok, errors = validate_payload(definition.output_schema, result_payload)
                if not ok:
                    message = "; ".join(errors)
                    self._record_failure(method_name, message)
                    raise SchemaValidationError(message)

        self._record_success(method_name, transport_result.duration_ms)
        return transport_result

    def _record_success(self, method_name: str, duration_ms: float | None) -> None:
        self._total_calls += 1
        metrics = self._method_metrics[method_name]
        metrics.calls += 1
        metrics.last_duration_ms = duration_ms
        metrics.last_error = None

    def _record_failure(self, method_name: str, error_message: str) -> None:
        self._total_calls += 1
        self._total_failures += 1
        metrics = self._method_metrics[method_name]
        metrics.calls += 1
        metrics.failures += 1
        metrics.last_error = error_message

    def get_diagnostics(self) -> Dict[str, Any]:
        diagnostics = {
            "enabled": self._enabled,
            "registered_methods": self._catalogue.list_methods(),
            "total_calls": self._total_calls,
            "total_failures": self._total_failures,
            "methods": {
                name: metrics.snapshot()
                for name, metrics in self._method_metrics.items()
            },
        }
        try:
            from .dynamic_tool_loader import get_dynamic_tool_registration_status

            diagnostics["dynamic_tool_registration"] = (
                get_dynamic_tool_registration_status()
            )
        except Exception as exc:
            diagnostics["dynamic_tool_registration"] = {
                "loaded_at_utc": None,
                "source": "vontology:#V#mcp_tool",
                "error": str(exc),
            }
        return diagnostics

    def describe_methods(self) -> Dict[str, Dict[str, Any]]:
        """Return descriptive metadata for registered methods."""

        return self._catalogue.snapshot()

    def get_method_definition(self, method_name: str) -> MethodDefinition | None:
        """Return the full method definition if registered."""

        try:
            return self._catalogue.get(method_name)
        except Exception:
            return None

    def extend_catalogue(self, definitions: Iterable[MethodDefinition]) -> None:
        for definition in definitions:
            self._catalogue.register(definition)
            self.register_metrics_if_missing(definition.name)
