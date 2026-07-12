"""Canonical identity support for represented required-tool names.

Represented workflow contracts may qualify an operation with its tool family
(``vontology:fetch_concept`` or ``arxiv:download_paper``), while execution
surfaces normally report the concrete method name (``fetch_concept`` or
``download_paper``).  Completion accounting must compare the operation rather
than raw spelling, without discarding the represented name or its qualifier.

This module deliberately owns identity and telemetry shape only.  It does not
decide which tools a workflow should require or whether an outcome is
semantically successful.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence


_LEGACY_OPERATION_ALIASES: Mapping[str, str] = {
    "search_concepts": "concept_search",
    "vontology_concept_search": "concept_search",
    "get_predicates_for_class": "get_predicate_incidence",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _registry_contract(tool_name: str) -> Any | None:
    """Resolve a canonical contract lazily to avoid import cycles."""

    try:
        from src.backend.integrations.internal_mcp.tool_contract_registry import (
            get_canonical_tool_registry,
        )

        return get_canonical_tool_registry().get(tool_name)
    except Exception:
        return None


def _qualified_parts(raw_name: str) -> tuple[str | None, str, str | None]:
    """Return qualifier, operation candidate, and qualification syntax."""

    lowered = raw_name.lower()
    if lowered.startswith("mcp__") and lowered.count("__") >= 2:
        _prefix, qualifier, operation = lowered.split("__", 2)
        if qualifier and operation:
            return qualifier, operation, "mcp_server"
    if ":" in lowered:
        qualifier, operation = lowered.split(":", 1)
        if qualifier and operation:
            return qualifier, operation, "family_colon"
    if "." in lowered:
        qualifier, operation = lowered.split(".", 1)
        if qualifier and operation:
            return qualifier, operation, "action_dot"
    return None, lowered, None


def _known_names(values: Iterable[Any] | None) -> set[str]:
    return {_clean(value).lower() for value in values or () if _clean(value)}


@dataclass(frozen=True)
class RequiredToolIdentity:
    """Traceable identity for one required, planned, or observed tool name."""

    original_name: str
    canonical_key: str
    operation_name: str
    family_qualifier: str | None = None
    qualification_syntax: str | None = None
    canonicalisation_source: str = "raw_name"
    resolution_status: str = "exact"
    unresolved_reason: str | None = None
    matched_known_surface_names: tuple[str, ...] = ()

    def to_telemetry(self) -> dict[str, Any]:
        return asdict(self)


def resolve_required_tool_identity(
    tool_name: Any,
    *,
    known_tool_names: Sequence[Any] | set[str] | None = None,
) -> RequiredToolIdentity:
    """Resolve a stable comparison key while retaining the authored name.

    A family qualifier is removed only when the unqualified operation is a
    canonical registered tool for that family. ``known_tool_names`` contributes
    traceability but is not authority to invent an undeclared family mapping.
    Dot-qualified action IDs are preserved when they are themselves the known
    surface; this prevents a generic suffix such as ``verify`` from accidentally
    conflating unrelated represented actions.
    """

    original = _clean(tool_name)
    lowered = original.lower()
    if not lowered:
        return RequiredToolIdentity(
            original_name="",
            canonical_key="",
            operation_name="",
        )

    qualifier, operation, syntax = _qualified_parts(lowered)
    known = _known_names(known_tool_names)
    direct_contract = _registry_contract(lowered)
    operation_contract = _registry_contract(operation) if operation else None

    canonical_operation = lowered
    source = "raw_name"
    resolution_status = "exact"
    unresolved_reason: str | None = None
    if direct_contract is not None:
        canonical_operation = _clean(getattr(direct_contract, "name", lowered)).lower()
        source = "canonical_tool_registry"
    elif qualifier and operation_contract is not None:
        contract_family = _clean(getattr(operation_contract, "family", "")).lower()
        if not contract_family or contract_family == qualifier:
            canonical_operation = _clean(
                getattr(operation_contract, "name", operation)
            ).lower()
            source = "family_qualified_registry_match"
            resolution_status = "resolved"
        else:
            resolution_status = "unresolved"
            unresolved_reason = "family_qualifier_does_not_match_registered_surface"
    elif qualifier and syntax in {"family_colon", "mcp_server"}:
        resolution_status = "unresolved"
        unresolved_reason = "qualified_operation_has_no_declared_identity"

    canonical_key = _LEGACY_OPERATION_ALIASES.get(
        canonical_operation,
        canonical_operation,
    )
    if canonical_key != canonical_operation:
        source = "declared_legacy_operation_alias"

    return RequiredToolIdentity(
        original_name=original,
        canonical_key=canonical_key,
        operation_name=canonical_operation,
        family_qualifier=qualifier,
        qualification_syntax=syntax,
        canonicalisation_source=source,
        resolution_status=resolution_status,
        unresolved_reason=unresolved_reason,
        matched_known_surface_names=tuple(
            sorted(
                name for name in known if name == lowered or name == canonical_operation
            )
        ),
    )


def canonical_required_tool_key(
    tool_name: Any,
    *,
    known_tool_names: Sequence[Any] | set[str] | None = None,
) -> str:
    return resolve_required_tool_identity(
        tool_name,
        known_tool_names=known_tool_names,
    ).canonical_key


def canonical_required_tool_keys(
    tool_names: Iterable[Any] | None,
    *,
    known_tool_names: Sequence[Any] | set[str] | None = None,
) -> set[str]:
    return {
        key
        for value in tool_names or ()
        if (
            key := canonical_required_tool_key(
                value,
                known_tool_names=known_tool_names,
            )
        )
    }


def required_tool_names_match(
    left: Any,
    right: Any,
    *,
    known_tool_names: Sequence[Any] | set[str] | None = None,
) -> bool:
    left_key = canonical_required_tool_key(
        left,
        known_tool_names=known_tool_names,
    )
    right_key = canonical_required_tool_key(
        right,
        known_tool_names=known_tool_names,
    )
    return bool(left_key and left_key == right_key)


__all__ = [
    "RequiredToolIdentity",
    "canonical_required_tool_key",
    "canonical_required_tool_keys",
    "required_tool_names_match",
    "resolve_required_tool_identity",
]
