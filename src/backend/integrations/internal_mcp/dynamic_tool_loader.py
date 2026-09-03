"""Dynamic MCP tool registration backed by Vontology concept metadata.

The runtime loads a constrained subset of ``#V#mcp_tool`` instances and turns
them into proxy ``MethodDefinition`` entries. The current implementation
intentionally fails closed:

- only explicitly enabled + approved concepts are considered;
- dynamic tools can proxy existing built-in handlers only;
- dynamic names cannot override protected built-in method names;
- malformed specs are rejected with structured diagnostics.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from .gateway import MethodDefinition
from .schemas import Schema, normalise_payload_aliases, validate_payload

logger = logging.getLogger(__name__)

_DYNAMIC_TOOL_INSTANCE_OF = "#V#mcp_tool"
_DYNAMIC_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,127}$")
_DYNAMIC_SPEC_CACHE_TTL_SECONDS = 60.0

_spec_cache_lock = threading.Lock()
_spec_cache_loaded = False
_spec_cache_timestamp = 0.0
_spec_cache_documents: list[dict[str, Any]] = []
_spec_cache_error: str | None = None

_status_lock = threading.Lock()
_last_registration_status: dict[str, Any] = {
    "loaded_at_utc": None,
    "source": "vontology:#V#mcp_tool",
    "total_concepts_scanned": 0,
    "candidate_count": 0,
    "loaded_count": 0,
    "failed_count": 0,
    "skipped_disabled_count": 0,
    "loaded": [],
    "failed": [],
    "source_load_error": None,
}


@dataclass(frozen=True)
class DynamicMethodLoadResult:
    """Result object for dynamic MCP method registration."""

    definitions: tuple[MethodDefinition, ...]
    status: dict[str, Any]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}
    return False


def _set_last_registration_status(status: Mapping[str, Any]) -> None:
    with _status_lock:
        global _last_registration_status
        _last_registration_status = copy.deepcopy(dict(status))


def get_dynamic_tool_registration_status() -> dict[str, Any]:
    """Return a safe snapshot of the most recent dynamic load attempt."""

    with _status_lock:
        return copy.deepcopy(_last_registration_status)


def reset_dynamic_tool_registration_status() -> None:
    """Reset dynamic registration diagnostics (test helper)."""

    _set_last_registration_status(
        {
            "loaded_at_utc": None,
            "source": "vontology:#V#mcp_tool",
            "total_concepts_scanned": 0,
            "candidate_count": 0,
            "loaded_count": 0,
            "failed_count": 0,
            "skipped_disabled_count": 0,
            "loaded": [],
            "failed": [],
            "source_load_error": None,
        }
    )


def invalidate_dynamic_tool_spec_cache() -> None:
    """Force refresh of Vontology-backed dynamic tool specs on next load."""

    with _spec_cache_lock:
        global _spec_cache_loaded
        global _spec_cache_timestamp
        global _spec_cache_documents
        global _spec_cache_error
        _spec_cache_loaded = False
        _spec_cache_timestamp = 0.0
        _spec_cache_documents = []
        _spec_cache_error = None


def _load_dynamic_tool_documents(
    *, force_refresh: bool = False
) -> tuple[list[dict[str, Any]], str | None]:
    global _spec_cache_loaded
    global _spec_cache_timestamp
    global _spec_cache_documents
    global _spec_cache_error

    now = time.time()
    with _spec_cache_lock:
        cache_fresh = (
            _spec_cache_loaded
            and not force_refresh
            and (now - _spec_cache_timestamp) < _DYNAMIC_SPEC_CACHE_TTL_SECONDS
        )
        if cache_fresh:
            return copy.deepcopy(_spec_cache_documents), _spec_cache_error

    documents: list[dict[str, Any]] = []
    load_error: str | None = None
    try:
        from ...db.repositories.concepts_repository import ConceptsRepository

        cursor = ConceptsRepository.find(
            {"relationships.is_an_instance_of": _DYNAMIC_TOOL_INSTANCE_OF},
            {"concept_id": 1, "attributes": 1},
        )
        for raw_doc in cursor:
            if isinstance(raw_doc, Mapping):
                documents.append(dict(raw_doc))
    except Exception as exc:
        load_error = str(exc)
        logger.warning(
            "[dynamic_tool_loader] Failed loading Vontology tool specs: %s", exc
        )

    with _spec_cache_lock:
        _spec_cache_loaded = True
        _spec_cache_timestamp = time.time()
        _spec_cache_documents = copy.deepcopy(documents)
        _spec_cache_error = load_error

    return documents, load_error


def _build_dynamic_input_schema(
    *,
    target_schema: Schema,
    fixed_payload_keys: set[str],
    tool_name: str,
    target_tool_name: str,
) -> Schema:
    remaining_fields = (
        set(target_schema.required) | set(target_schema.optional)
    ) - fixed_payload_keys
    required = {
        key: expected
        for key, expected in target_schema.required.items()
        if key not in fixed_payload_keys
    }
    optional = {
        key: expected
        for key, expected in target_schema.optional.items()
        if key not in fixed_payload_keys
    }
    description = (
        f"Dynamic tool '{tool_name}' proxying '{target_tool_name}'. "
        "Caller-provided values for fixed payload keys are not accepted."
    )
    return Schema(
        required=required,
        optional=optional,
        allow_unknown=target_schema.allow_unknown,
        description=description,
        aliases={
            alias_name: canonical_name
            for alias_name, canonical_name in target_schema.aliases.items()
            if alias_name not in fixed_payload_keys
            and canonical_name in remaining_fields
        },
        batch_propagated_fields=tuple(
            field_name
            for field_name in target_schema.batch_propagated_fields
            if field_name in remaining_fields
        ),
        enum_values={
            field_name: tuple(values)
            for field_name, values in target_schema.enum_values.items()
            if field_name in remaining_fields
        },
        scalar_source_fields={
            field_name: tuple(source_fields)
            for field_name, source_fields in target_schema.scalar_source_fields.items()
            if field_name in remaining_fields
        },
        comma_separated_list_fields=tuple(
            field_name
            for field_name in target_schema.comma_separated_list_fields
            if field_name in remaining_fields
        ),
        array_length_constraints={
            field_name: limits
            for field_name, limits in target_schema.array_length_constraints.items()
            if field_name in remaining_fields
        },
        array_item_schemas={
            field_name: item_schema
            for field_name, item_schema in target_schema.array_item_schemas.items()
            if field_name in remaining_fields
        },
    )


def _build_proxy_handler(
    *, target_handler: Callable[..., Any], fixed_payload: Mapping[str, Any]
) -> Callable[..., Any]:
    frozen_fixed_payload = dict(fixed_payload)

    def _dynamic_proxy_handler(**kwargs):
        merged_payload = dict(kwargs)
        # Fixed payload values always win so policy markers cannot be overridden.
        merged_payload.update(frozen_fixed_payload)
        return target_handler(**merged_payload)

    return _dynamic_proxy_handler


def _validate_fixed_payload(
    *,
    fixed_payload: Mapping[str, Any],
    target_tool_name: str,
    target_schema: Schema,
) -> list[str]:
    errors: list[str] = []

    for key in fixed_payload.keys():
        if not isinstance(key, str) or not key.strip():
            errors.append("dynamic_fixed_payload keys must be non-empty strings.")

    target_known_keys = set(target_schema.required.keys()) | set(
        target_schema.optional.keys()
    )
    if not target_schema.allow_unknown:
        unknown_keys = sorted(
            key for key in fixed_payload.keys() if key not in target_known_keys
        )
        if unknown_keys:
            errors.append(
                f"dynamic_fixed_payload contains unknown keys for '{target_tool_name}': {unknown_keys}"
            )

    typed_optional: dict[str, Any] = {}
    for key in fixed_payload.keys():
        expected = target_schema.expect(key)
        if expected is not None:
            typed_optional[key] = expected

    if typed_optional:
        ok, validation_errors = validate_payload(
            Schema(
                required={},
                optional=typed_optional,
                allow_unknown=target_schema.allow_unknown,
                description=(
                    f"dynamic_fixed_payload for tool '{target_tool_name}'"
                ),
            ),
            fixed_payload,
        )
        if not ok:
            errors.extend(validation_errors)

    return errors


def load_dynamic_method_definitions(
    *,
    base_definitions: Mapping[str, MethodDefinition],
    protected_method_names: Iterable[str] | None = None,
    force_refresh: bool = False,
) -> DynamicMethodLoadResult:
    """Resolve and validate dynamic method definitions from Vontology specs."""

    protected_names = {
        name.strip()
        for name in (protected_method_names or ())
        if isinstance(name, str) and name.strip()
    }
    protected_names.update(base_definitions.keys())

    documents, source_load_error = _load_dynamic_tool_documents(
        force_refresh=force_refresh
    )
    status: dict[str, Any] = {
        "loaded_at_utc": _utc_now_iso(),
        "source": "vontology:#V#mcp_tool",
        "total_concepts_scanned": len(documents),
        "candidate_count": 0,
        "loaded_count": 0,
        "failed_count": 0,
        "skipped_disabled_count": 0,
        "loaded": [],
        "failed": [],
        "source_load_error": source_load_error,
    }

    resolved: list[MethodDefinition] = []
    dynamic_name_guard: set[str] = set()

    for doc in documents:
        concept_id = str(doc.get("concept_id") or "").strip() or None
        attributes = doc.get("attributes")
        if not isinstance(attributes, Mapping):
            status["skipped_disabled_count"] += 1
            continue

        if not _is_truthy(attributes.get("dynamic_registration_enabled")):
            status["skipped_disabled_count"] += 1
            continue

        status["candidate_count"] += 1

        if not _is_truthy(attributes.get("dynamic_registration_approved")):
            status["failed"].append(
                {
                    "concept_id": concept_id,
                    "reason": "missing_approval_marker",
                    "message": (
                        "dynamic_registration_approved must be true for activation."
                    ),
                }
            )
            continue

        tool_name_raw = attributes.get("mcp_tool_name")
        tool_name = str(tool_name_raw or "").strip()
        if not tool_name:
            status["failed"].append(
                {
                    "concept_id": concept_id,
                    "reason": "missing_tool_name",
                    "message": "Missing attributes.mcp_tool_name",
                }
            )
            continue

        if not _DYNAMIC_TOOL_NAME_PATTERN.fullmatch(tool_name):
            status["failed"].append(
                {
                    "concept_id": concept_id,
                    "tool_name": tool_name,
                    "reason": "invalid_tool_name",
                    "message": (
                        "Tool name must match ^[a-z][a-z0-9_]{2,127}$."
                    ),
                }
            )
            continue

        if tool_name in protected_names:
            status["failed"].append(
                {
                    "concept_id": concept_id,
                    "tool_name": tool_name,
                    "reason": "protected_name_conflict",
                    "message": "Dynamic tool name conflicts with a protected method.",
                }
            )
            continue

        if tool_name in dynamic_name_guard:
            status["failed"].append(
                {
                    "concept_id": concept_id,
                    "tool_name": tool_name,
                    "reason": "duplicate_dynamic_name",
                    "message": (
                        "Another dynamic concept already registered this tool name."
                    ),
                }
            )
            continue

        target_tool_name = str(attributes.get("dynamic_target_tool_name") or "").strip()
        if not target_tool_name:
            status["failed"].append(
                {
                    "concept_id": concept_id,
                    "tool_name": tool_name,
                    "reason": "missing_target_tool_name",
                    "message": "Missing attributes.dynamic_target_tool_name",
                }
            )
            continue

        target_definition = base_definitions.get(target_tool_name)
        if target_definition is None:
            status["failed"].append(
                {
                    "concept_id": concept_id,
                    "tool_name": tool_name,
                    "target_tool_name": target_tool_name,
                    "reason": "target_not_registered",
                    "message": (
                        "dynamic_target_tool_name must reference an existing built-in method."
                    ),
                }
            )
            continue

        fixed_payload_raw = attributes.get("dynamic_fixed_payload")
        if fixed_payload_raw in (None, ""):
            fixed_payload: dict[str, Any] = {}
        elif isinstance(fixed_payload_raw, Mapping):
            fixed_payload = dict(fixed_payload_raw)
        else:
            status["failed"].append(
                {
                    "concept_id": concept_id,
                    "tool_name": tool_name,
                    "reason": "invalid_fixed_payload",
                    "message": "dynamic_fixed_payload must be a mapping when provided.",
                }
            )
            continue
        fixed_payload, _alias_warnings = normalise_payload_aliases(
            target_definition.input_schema,
            fixed_payload,
        )
        fixed_payload = dict(fixed_payload)

        fixed_payload_errors = _validate_fixed_payload(
            fixed_payload=fixed_payload,
            target_tool_name=target_tool_name,
            target_schema=target_definition.input_schema,
        )
        if fixed_payload_errors:
            status["failed"].append(
                {
                    "concept_id": concept_id,
                    "tool_name": tool_name,
                    "target_tool_name": target_tool_name,
                    "reason": "invalid_fixed_payload",
                    "message": "; ".join(fixed_payload_errors),
                }
            )
            continue

        trusted_bindings = (
            target_definition.ordinary_turn_trusted_argument_bindings
        )
        trusted_choice_bindings = (
            target_definition.ordinary_turn_trusted_argument_choice_bindings
        )
        trusted_argument_names = (
            {
                str(argument_name)
                for argument_name in trusted_bindings
                if isinstance(argument_name, str) and argument_name
            }
            if isinstance(trusted_bindings, Mapping)
            else set()
        )
        if isinstance(trusted_choice_bindings, Mapping):
            trusted_argument_names.update(
                str(argument_name)
                for argument_name in trusted_choice_bindings
                if isinstance(argument_name, str) and argument_name
            )
        fixed_trusted_argument_names = sorted(
            trusted_argument_names.intersection(
                str(key) for key in fixed_payload
            )
        )
        if fixed_trusted_argument_names:
            status["failed"].append(
                {
                    "concept_id": concept_id,
                    "tool_name": tool_name,
                    "target_tool_name": target_tool_name,
                    "reason": "fixed_payload_overrides_trusted_argument",
                    "message": (
                        "dynamic_fixed_payload cannot bind arguments supplied "
                        "from trusted ordinary-turn authority: "
                        f"{fixed_trusted_argument_names}"
                    ),
                    "conflicting_argument_names": (
                        fixed_trusted_argument_names
                    ),
                }
            )
            continue

        timeout_sec = target_definition.timeout_sec
        advisory_timeout_sec = target_definition.advisory_timeout_sec
        timeout_override = attributes.get("dynamic_timeout_sec")
        has_timeout_override = timeout_override not in (None, "")
        if has_timeout_override:
            if (
                isinstance(timeout_override, (int, float))
                and not isinstance(timeout_override, bool)
                and float(timeout_override) > 0.0
            ):
                # Legacy represented ``dynamic_timeout_sec`` values are
                # advisory. A proxy may inherit a target's independently
                # justified hard boundary, but a generic override must not
                # invent one.
                advisory_timeout_sec = float(timeout_override)
            else:
                status["failed"].append(
                    {
                        "concept_id": concept_id,
                        "tool_name": tool_name,
                        "reason": "invalid_timeout",
                        "message": "dynamic_timeout_sec must be a positive number.",
                    }
                )
                continue

        fixed_payload_keys = {str(key) for key in fixed_payload.keys()}
        dynamic_input_schema = _build_dynamic_input_schema(
            target_schema=target_definition.input_schema,
            fixed_payload_keys=fixed_payload_keys,
            tool_name=tool_name,
            target_tool_name=target_tool_name,
        )
        dynamic_description_raw = attributes.get("dynamic_description")
        dynamic_description = (
            dynamic_description_raw.strip()
            if isinstance(dynamic_description_raw, str)
            and dynamic_description_raw.strip()
            else None
        )
        description = dynamic_description or (
            f"Dynamic Vontology proxy for '{target_tool_name}'."
        )
        target_fixed_arguments = (
            dict(target_definition.ordinary_turn_fixed_arguments)
            if isinstance(
                target_definition.ordinary_turn_fixed_arguments,
                Mapping,
            )
            else {}
        )
        fixed_argument_conflict = any(
            argument_name in fixed_payload
            and fixed_payload.get(argument_name) != fixed_value
            for argument_name, fixed_value in target_fixed_arguments.items()
        )
        ordinary_turn_excluded_reason = (
            target_definition.ordinary_turn_excluded_reason
        )
        if fixed_argument_conflict and not ordinary_turn_excluded_reason:
            ordinary_turn_excluded_reason = (
                "dynamic_proxy_overrides_ordinary_turn_fixed_argument"
            )

        dynamic_definition = MethodDefinition(
            name=tool_name,
            handler=_build_proxy_handler(
                target_handler=target_definition.handler,
                fixed_payload=fixed_payload,
            ),
            input_schema=dynamic_input_schema,
            output_schema=target_definition.output_schema,
            category=target_definition.category,
            timeout_sec=timeout_sec,
            advisory_timeout_sec=advisory_timeout_sec,
            hard_timeout_enabled=target_definition.hard_timeout_enabled,
            successful_duration_bootstrap_sec=(
                None
                if has_timeout_override
                else target_definition.successful_duration_bootstrap_sec
            ),
            description=description,
            ordinary_turn_public=target_definition.ordinary_turn_public,
            ordinary_turn_excluded_reason=ordinary_turn_excluded_reason,
            ordinary_turn_trusted_argument_bindings=(
                dict(target_definition.ordinary_turn_trusted_argument_bindings)
                if isinstance(
                    target_definition.ordinary_turn_trusted_argument_bindings,
                    Mapping,
                )
                else None
            ),
            ordinary_turn_trusted_argument_choice_bindings=(
                dict(
                    target_definition.ordinary_turn_trusted_argument_choice_bindings
                )
                if isinstance(
                    target_definition.ordinary_turn_trusted_argument_choice_bindings,
                    Mapping,
                )
                else None
            ),
            ordinary_turn_fixed_arguments=target_fixed_arguments or None,
        )
        resolved.append(dynamic_definition)
        dynamic_name_guard.add(tool_name)
        status["loaded"].append(
            {
                "concept_id": concept_id,
                "tool_name": tool_name,
                "target_tool_name": target_tool_name,
                "category": target_definition.category,
                "fixed_payload_keys": sorted(fixed_payload_keys),
            }
        )

    status["loaded_count"] = len(resolved)
    status["failed_count"] = len(status["failed"])
    _set_last_registration_status(status)

    return DynamicMethodLoadResult(definitions=tuple(resolved), status=status)
