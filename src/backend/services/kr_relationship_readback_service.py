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
RELATIONSHIP_EFFECT_READBACK_SCHEMA_VERSION = "workflow_relationship_effect_readback.v1"
MAX_RELATIONSHIP_EFFECT_INVOCATIONS = 80
MAX_RELATIONSHIP_EFFECT_READBACK_HITS = 80


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


def _relationship_tuple(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, Mapping):
        return None

    def _first(*keys: str) -> str:
        return next(
            (
                cleaned
                for cleaned in (_clean_id(value.get(key)) for key in keys)
                if cleaned
            ),
            "",
        )

    source_id = _first("source_id", "concept_id", "subject_id")
    predicate_id = _first("predicate", "predicate_input", "predicate_concept_id")
    target_id = _first("target", "target_id", "object_concept_id")
    if not source_id or not predicate_id or not target_id:
        return None
    return source_id, predicate_id, target_id


def _invocation_tool_name(invocation: Mapping[str, Any]) -> str:
    for container in (
        invocation,
        invocation.get("effective_payload"),
        invocation.get("payload"),
    ):
        if not isinstance(container, Mapping):
            continue
        for key in ("tool", "method", "mcp_resolved_tool", "mcp_tool"):
            value = _clean_id(container.get(key))
            if value:
                return value
    return ""


def _invocation_transport_succeeded(invocation: Mapping[str, Any]) -> bool:
    if invocation.get("blocked") is True or _clean_id(invocation.get("error")):
        return False
    return _clean_id(invocation.get("status")).lower() in {
        "ok",
        "success",
        "succeeded",
        "completed",
    }


def _relationship_effect_readback_outcome(
    *,
    verified: bool,
    failure_code: str | None,
    mutation_tool_name: str,
    source_id: str,
    predicate_id: str,
    relation_kind: str,
    mutation_targets: Sequence[str] = (),
    matching_mutation_count: int = 0,
    readback_hit_count: int = 0,
    verified_relationship: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    represented_target_id = mutation_targets[0] if len(mutation_targets) == 1 else None
    return {
        "schema_version": RELATIONSHIP_EFFECT_READBACK_SCHEMA_VERSION,
        "relationship_effect_readback_verified": bool(verified),
        "relationship_effect_readback_failure_code": failure_code,
        "relationship_effect_mutation_tool": mutation_tool_name or None,
        "relationship_effect_expected_source_id": source_id or None,
        "relationship_effect_expected_predicate_id": predicate_id or None,
        "relationship_effect_expected_relation_kind": relation_kind or None,
        "relationship_effect_mutation_targets": list(mutation_targets),
        "relationship_effect_matching_mutation_count": int(matching_mutation_count),
        "relationship_effect_readback_hit_count": int(readback_hit_count),
        "represented_target_concept_id": represented_target_id,
        "verified_relationship": (
            dict(verified_relationship)
            if isinstance(verified_relationship, Mapping)
            else None
        ),
    }


def verify_relationship_effect_readback(
    *,
    mutation_tool_name: Any,
    expected_source_id: Any,
    expected_predicate_id: Any,
    expected_relation_kind: Any,
    tool_invocations: Any,
    readback_concept_id: Any,
    readback_total_hits: Any,
    readback_hits: Any,
    readback_total_hits_is_lower_bound: Any = False,
) -> dict[str, Any]:
    """Correlate a successful relationship write with exact canonical read-back.

    The expected source and predicate are workflow-authored. The target is
    derived from one coherent, result-confirmed mutation tuple from this run,
    then matched against one exact asserted relation hit. This prevents an
    earlier query or an unrelated pre-existing edge from satisfying the
    postcondition.
    """

    tool_name = _clean_id(mutation_tool_name)
    source_id = _clean_id(expected_source_id)
    predicate_id = _clean_id(expected_predicate_id)
    relation_kind = _clean_id(expected_relation_kind) or "binary"
    observed_readback_concept_id = _clean_id(readback_concept_id)
    invocations = (
        list(tool_invocations)
        if isinstance(tool_invocations, Sequence)
        and not isinstance(tool_invocations, (str, bytes, bytearray))
        else None
    )
    hits = (
        list(readback_hits)
        if isinstance(readback_hits, Sequence)
        and not isinstance(readback_hits, (str, bytes, bytearray))
        else None
    )
    try:
        total_hits = int(readback_total_hits)
    except (TypeError, ValueError):
        total_hits = -1

    if (
        not tool_name
        or not source_id
        or not predicate_id
        or not relation_kind
        or invocations is None
        or hits is None
        or total_hits < 0
    ):
        return _relationship_effect_readback_outcome(
            verified=False,
            failure_code="relationship_effect_readback_inputs_invalid",
            mutation_tool_name=tool_name,
            source_id=source_id,
            predicate_id=predicate_id,
            relation_kind=relation_kind,
        )
    if len(invocations) > MAX_RELATIONSHIP_EFFECT_INVOCATIONS:
        return _relationship_effect_readback_outcome(
            verified=False,
            failure_code="relationship_effect_invocation_bound_exceeded",
            mutation_tool_name=tool_name,
            source_id=source_id,
            predicate_id=predicate_id,
            relation_kind=relation_kind,
        )
    if len(hits) > MAX_RELATIONSHIP_EFFECT_READBACK_HITS:
        return _relationship_effect_readback_outcome(
            verified=False,
            failure_code="relationship_effect_readback_hit_bound_exceeded",
            mutation_tool_name=tool_name,
            source_id=source_id,
            predicate_id=predicate_id,
            relation_kind=relation_kind,
        )
    if observed_readback_concept_id != source_id:
        return _relationship_effect_readback_outcome(
            verified=False,
            failure_code="relationship_effect_readback_source_mismatch",
            mutation_tool_name=tool_name,
            source_id=source_id,
            predicate_id=predicate_id,
            relation_kind=relation_kind,
            readback_hit_count=len(hits),
        )
    if total_hits < len(hits):
        return _relationship_effect_readback_outcome(
            verified=False,
            failure_code="relationship_effect_readback_inputs_invalid",
            mutation_tool_name=tool_name,
            source_id=source_id,
            predicate_id=predicate_id,
            relation_kind=relation_kind,
            readback_hit_count=len(hits),
        )

    mutation_targets: list[str] = []
    matching_mutation_count = 0
    for invocation in invocations:
        if not isinstance(invocation, Mapping):
            continue
        if _invocation_tool_name(invocation).lower() != tool_name.lower():
            continue
        if not _invocation_transport_succeeded(invocation):
            continue
        effective_payload = invocation.get("effective_payload")
        if not isinstance(effective_payload, Mapping):
            continue
        if effective_payload.get("success") is not True:
            continue
        if _clean_id(effective_payload.get("relationship_type")) != "concept_relation":
            continue
        effect_tuple = _relationship_tuple(effective_payload)
        arguments_tuple = _relationship_tuple(invocation.get("effective_arguments"))
        if effect_tuple is None or arguments_tuple != effect_tuple:
            continue
        observed_source_id, observed_predicate_id, observed_target_id = effect_tuple
        if observed_source_id != source_id or observed_predicate_id != predicate_id:
            continue
        matching_mutation_count += 1
        mutation_targets.append(observed_target_id)

    unique_targets = list(dict.fromkeys(mutation_targets))
    if not unique_targets:
        return _relationship_effect_readback_outcome(
            verified=False,
            failure_code="relationship_effect_successful_mutation_missing",
            mutation_tool_name=tool_name,
            source_id=source_id,
            predicate_id=predicate_id,
            relation_kind=relation_kind,
            matching_mutation_count=matching_mutation_count,
            readback_hit_count=len(hits),
        )
    if len(unique_targets) != 1:
        return _relationship_effect_readback_outcome(
            verified=False,
            failure_code="relationship_effect_successful_mutation_ambiguous",
            mutation_tool_name=tool_name,
            source_id=source_id,
            predicate_id=predicate_id,
            relation_kind=relation_kind,
            mutation_targets=unique_targets,
            matching_mutation_count=matching_mutation_count,
            readback_hit_count=len(hits),
        )

    expected_target_id = unique_targets[0]
    for hit in hits:
        if not isinstance(hit, Mapping) or hit.get("access_granted") is False:
            continue
        relation_metadata = hit.get("relation_metadata")
        if hit.get("canonical_publication") is False or (
            isinstance(relation_metadata, Mapping)
            and relation_metadata.get("canonical_publication") is False
        ):
            continue
        if (
            hit.get("is_asserted") is not True
            or _clean_id(hit.get("relation_state")).lower() != "asserted"
        ):
            continue
        argument_indexes = hit.get("argument_indexes")
        if (
            not isinstance(argument_indexes, Sequence)
            or isinstance(argument_indexes, (str, bytes, bytearray))
            or 1 not in argument_indexes
        ):
            continue
        observed_target_id = _clean_id(
            hit.get("target_value") or hit.get("target_concept_id")
        )
        if (
            _clean_id(hit.get("source_concept_id")) != source_id
            or _clean_id(hit.get("predicate_concept_id")) != predicate_id
            or observed_target_id != expected_target_id
            or _clean_id(hit.get("relation_kind")) != relation_kind
        ):
            continue
        relation_id = (
            _clean_id(relation_metadata.get("relation_id"))
            if isinstance(relation_metadata, Mapping)
            else ""
        )
        return _relationship_effect_readback_outcome(
            verified=True,
            failure_code=None,
            mutation_tool_name=tool_name,
            source_id=source_id,
            predicate_id=predicate_id,
            relation_kind=relation_kind,
            mutation_targets=unique_targets,
            matching_mutation_count=matching_mutation_count,
            readback_hit_count=len(hits),
            verified_relationship={
                "source_id": source_id,
                "predicate_id": predicate_id,
                "target_id": expected_target_id,
                "relation_kind": relation_kind,
                "relation_id": relation_id or None,
            },
        )

    page_incomplete = bool(readback_total_hits_is_lower_bound) or total_hits > len(hits)
    return _relationship_effect_readback_outcome(
        verified=False,
        failure_code=(
            "relationship_effect_readback_incomplete"
            if page_incomplete
            else "relationship_effect_exact_readback_missing"
        ),
        mutation_tool_name=tool_name,
        source_id=source_id,
        predicate_id=predicate_id,
        relation_kind=relation_kind,
        mutation_targets=unique_targets,
        matching_mutation_count=matching_mutation_count,
        readback_hit_count=len(hits),
    )


__all__ = [
    "KR_RELATIONSHIP_READBACK_SCHEMA_VERSION",
    "MAX_RELATIONSHIP_EFFECT_INVOCATIONS",
    "MAX_RELATIONSHIP_EFFECT_READBACK_HITS",
    "RELATIONSHIP_EFFECT_READBACK_SCHEMA_VERSION",
    "verify_kr_relationship_readback",
    "verify_relationship_effect_readback",
]
