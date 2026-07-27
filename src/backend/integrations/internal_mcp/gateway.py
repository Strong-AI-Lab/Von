"""Core gateway primitives for the internal MCP integration."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, MutableMapping, Optional

from .schemas import (
    Schema,
    SchemaValidationError,
    coerce_payload_types,
    normalise_payload_aliases,
    validate_payload,
)
from .transport import (
    InternalMCPTransport,
    LateCompletionObserver,
    TransportResult,
)

logger = logging.getLogger(__name__)

_LOG_TAG = "[mcp_gateway]"
_ACTOR_CONTEXT_SOURCE: ContextVar[str | None] = ContextVar(
    "internal_mcp_actor_context_source",
    default=None,
)
_PREEXISTING_ACTOR_CONTEXT: ContextVar[
    tuple[str | None, str | None] | None
] = ContextVar(
    "internal_mcp_preexisting_actor_context",
    default=None,
)


def get_internal_mcp_actor_context_source() -> str | None:
    """Return whether the active actor pre-existed or came from tool payload."""

    return _ACTOR_CONTEXT_SOURCE.get()


def get_internal_mcp_preexisting_actor_context() -> (
    tuple[str | None, str | None] | None
):
    """Return actor authority present before tool-payload fallback was applied."""

    return _PREEXISTING_ACTOR_CONTEXT.get()


@contextmanager
def bind_internal_mcp_actor_context_source(
    source: str,
    *,
    preexisting_actor_context: tuple[str | None, str | None] | None = None,
) -> Iterator[None]:
    """Mark a non-gateway proxy with the same bounded actor provenance.

    Some compatibility surfaces call catalogue handlers directly. Sensitive
    handlers still need to distinguish unauthenticated payload claims from an
    actor that existed before the tool call, so those proxies must bind this
    provenance explicitly rather than being mistaken for trusted in-process
    calls.
    """

    source_token = _ACTOR_CONTEXT_SOURCE.set(str(source or "").strip() or None)
    actor_token = _PREEXISTING_ACTOR_CONTEXT.set(preexisting_actor_context)
    try:
        yield
    finally:
        _PREEXISTING_ACTOR_CONTEXT.reset(actor_token)
        _ACTOR_CONTEXT_SOURCE.reset(source_token)


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
    ordinary_turn_public: bool = False
    # Evidence-backed authority/effect exception only. This must not encode
    # request relevance, preferred routing, or a prescribed solution path.
    ordinary_turn_excluded_reason: str | None = None
    # Map capability arguments to values resolved by the trusted entry point.
    ordinary_turn_trusted_argument_bindings: Mapping[str, str] | None = None
    # Evidence-backed option-level boundary. The model cannot see or override
    # these values on an ordinary turn; keep the rest of the capability usable.
    ordinary_turn_fixed_arguments: Mapping[str, Any] | None = None
    # Explicitly delegate this write as a bounded ordinary-turn semantic effect.
    # This is an access/effect ceiling, not a request classifier or preferred
    # solution route.
    ordinary_turn_effect: bool = False
    # Existing concepts may be changed only when this authoritative forward
    # subject is scoped to the trusted actor or organisation. Creation effects
    # leave this unset because their scope is fixed server-side.
    ordinary_turn_mutation_subject_argument: str | None = None

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
    timeouts: int = 0
    saturations: int = 0
    last_error: str | None = None
    last_outcome: str | None = None
    last_duration_ms: float | None = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "saturations": self.saturations,
            "last_error": self.last_error,
            "last_outcome": self.last_outcome,
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
            "aliases": dict(schema.aliases),
            "batch_propagated_fields": [
                field_name
                for field_name in schema.batch_propagated_fields
                if isinstance(field_name, str) and field_name
            ],
            "enum_values": {
                field_name: list(values)
                for field_name, values in schema.enum_values.items()
                if isinstance(field_name, str) and field_name
            },
            "scalar_source_fields": {
                field_name: [
                    source_field
                    for source_field in source_fields
                    if isinstance(source_field, str) and source_field
                ]
                for field_name, source_fields in schema.scalar_source_fields.items()
                if isinstance(field_name, str) and field_name
            },
            "comma_separated_list_fields": [
                field_name
                for field_name in schema.comma_separated_list_fields
                if isinstance(field_name, str) and field_name
            ],
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
                "ordinary_turn_public": definition.ordinary_turn_public,
                "ordinary_turn_excluded_reason": (
                    definition.ordinary_turn_excluded_reason
                ),
                "ordinary_turn_trusted_argument_bindings": (
                    dict(definition.ordinary_turn_trusted_argument_bindings)
                    if isinstance(
                        definition.ordinary_turn_trusted_argument_bindings,
                        Mapping,
                    )
                    else None
                ),
                "ordinary_turn_fixed_arguments": (
                    dict(definition.ordinary_turn_fixed_arguments)
                    if isinstance(
                        definition.ordinary_turn_fixed_arguments,
                        Mapping,
                    )
                    else None
                ),
                "ordinary_turn_effect": definition.ordinary_turn_effect,
                "ordinary_turn_mutation_subject_argument": (
                    definition.ordinary_turn_mutation_subject_argument
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
        trusted_actor_payload_fallback: bool = False,
    ) -> None:
        self._catalogue = catalogue
        self._transport = transport
        self._enabled = bool(enabled)
        self._log_tag = log_tag
        self._trusted_actor_payload_fallback = bool(
            trusted_actor_payload_fallback
        )
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

    @staticmethod
    def _clean_concept_id(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        return cleaned if cleaned.startswith("#V#") else None

    @classmethod
    def _resolve_access_actor_context(
        cls,
        payload: Mapping[str, Any],
    ) -> tuple[str | None, str | None]:
        user_id = cls._clean_concept_id(
            payload.get("user_concept_id") or payload.get("user_id")
        )
        org_id = cls._clean_concept_id(
            payload.get("organisation_concept_id")
            or payload.get("org_concept_id")
            or payload.get("organisation_id")
            or payload.get("org_id")
        )
        namespace = payload.get("namespace")
        if isinstance(namespace, str) and namespace.strip():
            try:
                from src.backend.services.namespace_service import (
                    derive_actor_context_from_namespace,
                )

                namespace_user, namespace_org = derive_actor_context_from_namespace(
                    namespace
                )
            except Exception:
                namespace_user, namespace_org = None, None
            user_id = user_id or cls._clean_concept_id(namespace_user)
            org_id = org_id or cls._clean_concept_id(namespace_org)
        return user_id, org_id

    @staticmethod
    @contextmanager
    def _access_actor_context(
        user_id: str | None,
        org_id: str | None,
    ) -> Iterator[None]:
        if not user_id and not org_id:
            yield
            return
        from src.backend.security.access_control import (
            override_current_organisation,
            override_current_user,
        )

        with override_current_user(user_id), override_current_organisation(org_id):
            yield

    def invoke(
        self,
        method_name: str,
        payload: Optional[MutableMapping[str, Any]] = None,
        *,
        deadline_monotonic: float | None = None,
        require_configured_timeout: bool = False,
        late_completion_observer: LateCompletionObserver | None = None,
    ) -> TransportResult:
        if not self._enabled:
            raise GatewayDisabledError("Internal MCP gateway is disabled.")

        payload_dict: MutableMapping[str, Any] = dict(payload or {})
        definition = self._catalogue.get(method_name)
        self.register_metrics_if_missing(method_name)

        payload_dict, alias_warnings = normalise_payload_aliases(
            definition.input_schema,
            payload_dict,
        )
        if alias_warnings:
            logger.debug(
                "%s normalised aliases for %s: %s",
                self._log_tag,
                method_name,
                "; ".join(alias_warnings),
            )

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
            raise SchemaValidationError(error_message, stage="input_schema")

        timeout = definition.resolved_timeout(self._transport)
        advisory_timeout = self._transport.advisory_timeout_sec(
            definition.category,
            hard_timeout_sec=float(timeout or self._transport.read_timeout_sec),
        )
        observed_late_completion = late_completion_observer
        if late_completion_observer is not None:

            def _observe_validated_late_completion(
                observation: Dict[str, Any],
            ) -> None:
                enriched = dict(observation)
                result_payload = enriched.get("payload")
                if enriched.get("outcome") != "late_success":
                    validation = "handler_error"
                    valid: bool | None = None
                    validation_error = None
                elif enriched.get("payload_truncated") is True:
                    validation = "indeterminate_truncated"
                    valid = None
                    validation_error = "Late handler payload was truncated."
                elif definition.output_schema is None:
                    validation = "not_configured"
                    valid = None
                    validation_error = None
                elif not isinstance(result_payload, MutableMapping):
                    validation = "invalid"
                    valid = False
                    validation_error = (
                        "Output schema provided but late handler returned "
                        "a non-mapping payload."
                    )
                elif result_payload.get("success") is False:
                    validation = "standard_error_response"
                    valid = True
                    validation_error = None
                else:
                    try:
                        valid, validation_errors = validate_payload(
                            definition.output_schema,
                            result_payload,
                        )
                    except Exception as exc:
                        valid = False
                        validation_errors = [type(exc).__name__]
                    validation = "valid" if valid else "invalid"
                    validation_error = (
                        None
                        if valid
                        else "; ".join(validation_errors)[:2_000]
                    )
                enriched.update(
                    {
                        "output_schema_validation": validation,
                        "output_schema_valid": valid,
                        "output_schema_error": validation_error,
                    }
                )
                late_completion_observer(enriched)

            observed_late_completion = _observe_validated_late_completion
        try:
            from src.backend.security.access_control import (
                get_effective_organisation_concept_id,
                get_effective_user_concept_id,
            )

            existing_user_id = get_effective_user_concept_id()
            existing_org_id = get_effective_organisation_concept_id()
            payload_user_id, payload_org_id = self._resolve_access_actor_context(
                payload_dict
            )
            preexisting_actor_context = (
                (existing_user_id, existing_org_id)
                if existing_user_id or existing_org_id
                else None
            )
            if preexisting_actor_context is not None:
                # Ambient authentication/workflow authority is one indivisible
                # actor scope.  Do not let a tool payload fill a missing user or
                # organisation component and thereby widen access for listing,
                # discovery, or any other handler that consumes the context.
                user_id, org_id = preexisting_actor_context
            else:
                user_id, org_id = payload_user_id, payload_org_id
            actor_source = (
                "preexisting_authenticated_or_workflow_context"
                if preexisting_actor_context is not None
                else (
                    "trusted_operator_payload_fallback"
                    if self._trusted_actor_payload_fallback
                    else "tool_payload_fallback"
                )
            )
            actor_source_token = _ACTOR_CONTEXT_SOURCE.set(actor_source)
            preexisting_actor_token = _PREEXISTING_ACTOR_CONTEXT.set(
                preexisting_actor_context
            )
            try:
                with self._access_actor_context(user_id, org_id):
                    # A trusted in-process AgentTest harness may bind a
                    # represented, context-scoped fault plan.  Resolve and bind
                    # the exact authenticated actor before applying it so a
                    # synthetic transport fact cannot bypass actor-scope
                    # establishment.  The hook owns no scenario or recovery
                    # policy and accepts no payload control fields.
                    from .agent_test_fault_plan import (
                        maybe_inject_agent_test_mcp_fault,
                    )

                    injected_fault = maybe_inject_agent_test_mcp_fault(
                        method_name
                    )
                    if injected_fault is not None:
                        self._record_failure(
                            method_name,
                            injected_fault.error_code,
                        )
                        logger.warning(
                            "%s AgentTest fault plan injected %s for %s "
                            "(fault_id=%s)",
                            self._log_tag,
                            injected_fault.event.get("fault_class"),
                            method_name,
                            injected_fault.event.get("fault_id"),
                        )
                        return TransportResult(
                            payload=dict(injected_fault.payload),
                            duration_ms=0.0,
                        )
                    transport_result = self._transport.execute(
                        method_name=definition.name,
                        handler=definition.handler,
                        payload=dict(payload_dict),
                        timeout_sec=timeout,
                        category=definition.category,
                        advisory_timeout_sec=advisory_timeout,
                        deadline_monotonic=deadline_monotonic,
                        require_configured_timeout=require_configured_timeout,
                        log_tag=self._log_tag,
                        late_completion_observer=observed_late_completion,
                    )
            finally:
                _PREEXISTING_ACTOR_CONTEXT.reset(preexisting_actor_token)
                _ACTOR_CONTEXT_SOURCE.reset(actor_source_token)
        except Exception as exc:
            message = str(exc)
            logger.exception(
                "%s handler for %s raised: %s", self._log_tag, method_name, message
            )
            self._record_failure(method_name, message)
            raise

        result_payload = transport_result.payload
        if transport_result.outcome != "completed":
            error_code = (
                str(result_payload.get("error_code") or transport_result.outcome)
                if isinstance(result_payload, Mapping)
                else transport_result.outcome
            )
            self._record_failure(
                method_name,
                error_code,
                duration_ms=transport_result.duration_ms,
                outcome=transport_result.outcome,
            )
            return transport_result

        if definition.output_schema is not None:
            if not isinstance(result_payload, MutableMapping):
                message = (
                    "Output schema provided but handler returned non-mapping payload."
                )
                self._record_failure(method_name, message)
                raise SchemaValidationError(message, stage="output_schema")
            # Skip output schema validation for error responses (MCPErrorResponse)
            # Error responses follow a different standardised schema with success=False
            is_error_response = result_payload.get("success") is False
            if not is_error_response:
                ok, errors = validate_payload(definition.output_schema, result_payload)
                if not ok:
                    message = "; ".join(errors)
                    self._record_failure(method_name, message)
                    raise SchemaValidationError(message, stage="output_schema")

        self._record_success(method_name, transport_result.duration_ms)
        return transport_result

    def _record_success(self, method_name: str, duration_ms: float | None) -> None:
        self._total_calls += 1
        metrics = self._method_metrics[method_name]
        metrics.calls += 1
        metrics.last_duration_ms = duration_ms
        metrics.last_error = None
        metrics.last_outcome = "completed"

    def _record_failure(
        self,
        method_name: str,
        error_message: str,
        *,
        duration_ms: float | None = None,
        outcome: str = "failed",
    ) -> None:
        self._total_calls += 1
        self._total_failures += 1
        metrics = self._method_metrics[method_name]
        metrics.calls += 1
        metrics.failures += 1
        metrics.last_error = error_message
        metrics.last_duration_ms = duration_ms
        metrics.last_outcome = outcome
        if outcome == "timed_out":
            metrics.timeouts += 1
        elif outcome == "saturated":
            metrics.saturations += 1

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
        try:
            diagnostics["transport"] = self._transport.get_diagnostics()
        except Exception as exc:
            diagnostics["transport"] = {
                "status": "unavailable",
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

    def get_method_timeout_sec(self, method_name: str) -> float | None:
        """Return the configured hard window for one registered method."""

        definition = self.get_method_definition(method_name)
        return (
            definition.resolved_timeout(self._transport)
            if definition is not None
            else None
        )

    def extend_catalogue(self, definitions: Iterable[MethodDefinition]) -> None:
        for definition in definitions:
            self._catalogue.register(definition)
            self.register_metrics_if_missing(definition.name)
