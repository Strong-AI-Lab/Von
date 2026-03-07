"""Shared helpers for deterministic workflow context path resolution.

Keep context-path lookup central so transition conditions, iterator actions,
and idempotency policies all resolve workflow data the same way.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


_NESTED_CONTEXT_KEYS: tuple[str, ...] = (
    "facts",
    "state",
    "flags",
    "variables",
    "effects",
    "preconditions",
    "conditions",
)


def _normalise_root_candidates(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    candidates = [text]
    if text.startswith("#V#") and len(text) > 3:
        candidates.append(text[3:])
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _split_path(path: str) -> list[str]:
    return [part.strip() for part in str(path or "").split(".") if part.strip()]


def _lookup_parts(root: Any, parts: Sequence[str]) -> tuple[bool, Any]:
    current = root
    for part in parts:
        if isinstance(current, Mapping):
            if part not in current:
                return False, None
            current = current.get(part)
            continue
        if isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            try:
                index = int(part)
            except (TypeError, ValueError):
                return False, None
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def resolve_context_path(
    *,
    context: Mapping[str, Any],
    path: str,
) -> tuple[bool, Any]:
    """Resolve a direct or dotted path from workflow context."""

    path_text = str(path or "").strip()
    if not path_text:
        return False, None

    if path_text in context:
        return True, context.get(path_text)

    parts = _split_path(path_text)
    if not parts:
        return False, None

    root_candidates = _normalise_root_candidates(parts[0])
    remainder = parts[1:]

    for root_key in root_candidates:
        if root_key in context:
            if not remainder:
                return True, context.get(root_key)
            found, value = _lookup_parts(context.get(root_key), remainder)
            if found:
                return True, value

    for container_key in _NESTED_CONTEXT_KEYS:
        nested = context.get(container_key)
        if not isinstance(nested, Mapping):
            continue
        for root_key in root_candidates:
            if root_key in nested:
                if not remainder:
                    return True, nested.get(root_key)
                found, value = _lookup_parts(nested.get(root_key), remainder)
                if found:
                    return True, value

    return False, None


def measure_cardinality(value: Any) -> int | None:
    """Return the cardinality for list-like values used in workflow guards."""

    if value is None:
        return 0
    if isinstance(value, Mapping):
        return len(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value)
    if isinstance(value, (set, frozenset)):
        return len(value)
    return None


__all__ = ["measure_cardinality", "resolve_context_path"]
