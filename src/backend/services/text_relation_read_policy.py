"""Neutral visibility policy for generic text-relation reads.

Authentication bindings are represented as text relations so the dedicated
authentication and administration services can manage them canonically.  They
are not ordinary ontology content, however, and must not be projected through
generic knowledge-reading tools.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

GENERIC_TEXT_READ_HIDDEN_PREDICATES = frozenset({"#V#hasVonLoginEmail"})


def _normalise_predicate(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _predicate_aliases(predicate: str) -> frozenset[str]:
    if predicate.startswith("#V#"):
        return frozenset({predicate, predicate[3:]})
    return frozenset({predicate, f"#V#{predicate}"})


_GENERIC_TEXT_READ_HIDDEN_QUERY_VALUES = frozenset(
    alias
    for predicate in GENERIC_TEXT_READ_HIDDEN_PREDICATES
    for alias in _predicate_aliases(predicate)
)


def is_hidden_from_generic_text_reads(predicate: object) -> bool:
    """Return whether ``predicate`` is reserved from generic text reads."""

    value = _normalise_predicate(predicate)
    if not value:
        return False
    return bool(_predicate_aliases(value) & GENERIC_TEXT_READ_HIDDEN_PREDICATES)


def text_contains_hidden_generic_predicate(value: object) -> bool:
    """Return whether text names a predicate reserved from generic reads."""

    return isinstance(value, str) and any(
        predicate in value for predicate in _GENERIC_TEXT_READ_HIDDEN_QUERY_VALUES
    )


def generic_text_read_predicate_filter(
    *,
    predicate: object = None,
    predicates: Sequence[object] | None = None,
) -> str | dict[str, list[str]]:
    """Build a Mongo predicate condition that excludes reserved read rows.

    A scalar request takes precedence, matching the existing generic text-read
    contract.  An explicitly requested hidden predicate becomes an empty
    ``$in`` query; an unfiltered read gets an explicit ``$nin`` constraint so
    hidden rows cannot affect counts, limits, or truncation diagnostics.
    """

    scalar = _normalise_predicate(predicate)
    if scalar:
        if is_hidden_from_generic_text_reads(scalar):
            return {"$in": []}
        return scalar

    requested = [
        value
        for raw_value in predicates or ()
        if (value := _normalise_predicate(raw_value))
    ]
    if requested:
        return {
            "$in": [
                value
                for value in requested
                if not is_hidden_from_generic_text_reads(value)
            ]
        }

    return {"$nin": sorted(_GENERIC_TEXT_READ_HIDDEN_QUERY_VALUES)}


def filter_generic_text_read_rows(
    rows: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """Remove reserved predicates from an already materialised generic read."""

    return [
        row
        for row in rows
        if not is_hidden_from_generic_text_reads(row.get("predicate"))
    ]


__all__ = [
    "GENERIC_TEXT_READ_HIDDEN_PREDICATES",
    "filter_generic_text_read_rows",
    "generic_text_read_predicate_filter",
    "is_hidden_from_generic_text_reads",
    "text_contains_hidden_generic_predicate",
]
