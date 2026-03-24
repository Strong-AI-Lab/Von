"""Helpers for VWL step-local static input bindings.

Structured step-local defaults are authored as JSON-compatible values in
workflow metadata, serialised into ``hasInputMap`` text relations, and restored
to native Python values when the workflow definition is loaded.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

STATIC_INPUT_JSON_PREFIX = "json:"


def normalise_static_input_binding_value(value: Any) -> Any | None:
    """Return a stable JSON-compatible binding value or ``None`` when invalid."""

    if isinstance(value, str):
        text = value.strip()
        return text or None
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        try:
            return json.loads(json.dumps(dict(value), ensure_ascii=True, sort_keys=True))
        except (TypeError, ValueError):
            return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(json.dumps(list(value), ensure_ascii=True, sort_keys=True))
        except (TypeError, ValueError):
            return None
    return None


def coerce_static_input_binding(key: Any, value: Any) -> tuple[str, Any] | None:
    key_text = str(key or "").strip()
    if not key_text:
        return None
    normalised_value = normalise_static_input_binding_value(value)
    if normalised_value is None:
        return None
    return key_text, normalised_value


def serialise_static_input_binding_value(value: Any) -> str | None:
    normalised_value = normalise_static_input_binding_value(value)
    if normalised_value is None:
        return None
    if isinstance(normalised_value, str):
        return normalised_value
    return STATIC_INPUT_JSON_PREFIX + json.dumps(
        normalised_value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_static_input_binding_value(value: Any) -> Any | None:
    if not isinstance(value, str):
        return copy.deepcopy(value)
    text = value.strip()
    if not text:
        return None
    if not text.startswith(STATIC_INPUT_JSON_PREFIX):
        return text
    raw_json = text[len(STATIC_INPUT_JSON_PREFIX) :].strip()
    if not raw_json:
        return None
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        return text


def static_input_binding_signature(binding: tuple[str, Any]) -> str:
    key, value = binding
    normalised = coerce_static_input_binding(key, value)
    if normalised is None:
        return ""
    key_text, normalised_value = normalised
    if isinstance(normalised_value, str):
        value_signature = f"str:{normalised_value}"
    else:
        value_signature = json.dumps(
            normalised_value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    return f"{key_text}={value_signature}"


def dedupe_static_input_bindings(
    bindings: Sequence[tuple[str, Any]],
) -> tuple[tuple[str, Any], ...]:
    deduped: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for binding in bindings:
        normalised = coerce_static_input_binding(binding[0], binding[1])
        if normalised is None:
            continue
        signature = static_input_binding_signature(normalised)
        if not signature or signature in seen:
            continue
        seen.add(signature)
        deduped.append(normalised)
    return tuple(deduped)


def stable_static_input_bindings(
    bindings: Sequence[tuple[str, Any]],
) -> tuple[tuple[str, Any], ...]:
    deduped = dedupe_static_input_bindings(bindings)
    return tuple(sorted(deduped, key=static_input_binding_signature))


__all__ = [
    "STATIC_INPUT_JSON_PREFIX",
    "coerce_static_input_binding",
    "dedupe_static_input_bindings",
    "normalise_static_input_binding_value",
    "parse_static_input_binding_value",
    "serialise_static_input_binding_value",
    "stable_static_input_bindings",
    "static_input_binding_signature",
]
