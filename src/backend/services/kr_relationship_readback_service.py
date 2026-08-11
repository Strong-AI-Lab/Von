"""Deterministic verification for one KR relationship read-back.

The KR relationship item workflow already reads both endpoint concepts after an
``add_relationship`` call.  This module verifies that those observations prove
the exact requested source/predicate/target tuple; matching the endpoint IDs or
the predicate independently is not enough.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

KR_RELATIONSHIP_READBACK_SCHEMA_VERSION = "kr_relationship_readback.v1"


def _clean_id(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _canonical_predicate_id(value: Any) -> str:
    predicate_id = _clean_id(value)
    if not predicate_id:
        return ""
    # Keep the common exact custom-predicate path independent of Vontology
    # metadata loading. Canonicalisation is needed only when the write path may
    # have stored a structural predicate under its field-name alias.
    from .relationship_write_service import normalise_structural_predicate

    normalised = normalise_structural_predicate(predicate_id)
    return _clean_id(normalised) or predicate_id


def _relationship_targets(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates: Sequence[Any] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        candidates = value
    else:
        return ()
    return tuple(
        dict.fromkeys(
            target
            for target in (_clean_id(candidate) for candidate in candidates)
            if target
        )
    )


def verify_kr_relationship_readback(
    *,
    expected_source_id: Any,
    expected_predicate_id: Any,
    expected_target_id: Any,
    assertion_succeeded: Any,
    source_readback_id: Any,
    source_readback_relationships: Any,
    target_readback_id: Any,
) -> dict[str, Any]:
    """Return exact, fail-closed evidence for one requested KR relationship.

    ``fetch_concept`` stores structural predicates under their canonical field
    names, so represented aliases such as ``#V#is_a_type_of`` are compared via
    the same canonicaliser used by the relationship write path.  Non-structural
    predicates remain exact concept-ID comparisons.
    """

    source_id = _clean_id(expected_source_id)
    predicate_id = _clean_id(expected_predicate_id)
    target_id = _clean_id(expected_target_id)
    observed_source_id = _clean_id(source_readback_id)
    observed_target_id = _clean_id(target_readback_id)

    expected = {
        "source_id": source_id,
        "predicate_id": predicate_id,
        "target_id": target_id,
    }
    observed: dict[str, Any] = {
        "assertion_succeeded": assertion_succeeded is True,
        "source_readback_id": observed_source_id or None,
        "target_readback_id": observed_target_id or None,
        "matched_predicate_keys": [],
    }

    def _outcome(failure_code: str | None) -> dict[str, Any]:
        verified = failure_code is None
        payload: dict[str, Any] = {
            "schema_version": KR_RELATIONSHIP_READBACK_SCHEMA_VERSION,
            "success": verified,
            "verified": verified,
            "failure_code": failure_code,
            "expected_relationship": expected,
            "observed_readback": observed,
        }
        if verified:
            payload["verified_relationship"] = dict(expected)
        return payload

    if not source_id or not predicate_id or not target_id:
        return _outcome("kr_relationship_readback_expectation_invalid")
    if assertion_succeeded is not True:
        return _outcome("kr_relationship_assertion_not_succeeded")
    if observed_source_id != source_id:
        return _outcome("kr_relationship_source_readback_mismatch")
    if observed_target_id != target_id:
        return _outcome("kr_relationship_target_readback_mismatch")
    if not isinstance(source_readback_relationships, Mapping):
        return _outcome("kr_relationship_source_relationships_missing")

    matched_predicate_keys: list[str] = []
    exact_targets = source_readback_relationships.get(predicate_id)
    edge_observed = target_id in _relationship_targets(exact_targets)
    if predicate_id in source_readback_relationships:
        matched_predicate_keys.append(predicate_id)
    canonical_predicate_id: str | None = None
    for raw_predicate_key, raw_targets in (
        () if edge_observed else source_readback_relationships.items()
    ):
        predicate_key = _clean_id(raw_predicate_key)
        if not predicate_key or predicate_key == predicate_id:
            continue
        if canonical_predicate_id is None:
            canonical_predicate_id = _canonical_predicate_id(predicate_id)
        if _canonical_predicate_id(predicate_key) != canonical_predicate_id:
            continue
        matched_predicate_keys.append(predicate_key)
        if target_id in _relationship_targets(raw_targets):
            edge_observed = True

    observed["matched_predicate_keys"] = list(dict.fromkeys(matched_predicate_keys))
    if not edge_observed:
        return _outcome("kr_relationship_edge_readback_missing")
    return _outcome(None)


__all__ = [
    "KR_RELATIONSHIP_READBACK_SCHEMA_VERSION",
    "verify_kr_relationship_readback",
]
