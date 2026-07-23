"""Generic deterministic guard for bounded KR materialisation writes.

The guard contract is caller-authored represented policy.  This module owns
only its hard schema and comparison semantics; it does not contain domain
concepts, predicates, or naming policy.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any


KR_MATERIALISATION_GUARD_SCHEMA_VERSION = "kr_materialisation_guard.v1"
_MAX_GUARD_CONCEPT_SLOTS = 256
_MAX_GUARD_RELATIONSHIP_RULES = 512
_MAX_DESCRIPTION_CHARS = 100_000


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sequence(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return []
    return list(value)


def _reject(code: str, *, phase: str) -> dict[str, Any]:
    return {
        "success": True,
        "guard_applied": True,
        "guard_passed": False,
        "guard_phase": phase,
        "error_code": code,
        "blocking_reason": code,
    }


def _pass(
    *,
    phase: str,
    guard_applied: bool,
    concept_count: int = 0,
    relationship_count: int = 0,
) -> dict[str, Any]:
    return {
        "success": True,
        "guard_applied": guard_applied,
        "guard_passed": True,
        "guard_phase": phase,
        "concept_spec_count": concept_count,
        "relationship_spec_count": relationship_count,
        "blocking_reason": None,
    }


def _normalise_contract(
    guard_contract: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if (
        _text(guard_contract.get("schema_version"))
        != KR_MATERIALISATION_GUARD_SCHEMA_VERSION
    ):
        return None, "kr_materialisation_guard_schema_unsupported"

    raw_slots = _sequence(guard_contract.get("concept_slots"))
    raw_rules = _sequence(guard_contract.get("relationship_rules"))
    if (
        not raw_slots
        or len(raw_slots) > _MAX_GUARD_CONCEPT_SLOTS
        or len(raw_rules) > _MAX_GUARD_RELATIONSHIP_RULES
    ):
        return None, "kr_materialisation_guard_contract_bounds_invalid"

    slots: dict[str, dict[str, Any]] = {}
    for raw_slot in raw_slots:
        if not isinstance(raw_slot, Mapping):
            return None, "kr_materialisation_guard_concept_slot_invalid"
        key = _text(raw_slot.get("key"))
        stable_name = _text(raw_slot.get("stable_name"))
        parent_id = _text(raw_slot.get("parent_id"))
        target_kind = _text(raw_slot.get("target_kind")).lower()
        allowed_decisions = {
            _text(item).lower()
            for item in _sequence(raw_slot.get("allowed_decisions") or ["create"])
            if _text(item).lower() in {"create", "reuse_existing"}
        }
        allowed_existing_ids = {
            _text(item)
            for item in _sequence(raw_slot.get("allowed_existing_concept_ids"))
            if _text(item)
        }
        if (
            not key
            or key in slots
            or not stable_name
            or not parent_id
            or target_kind not in {"type", "instance", "predicate"}
            or not allowed_decisions
        ):
            return None, "kr_materialisation_guard_concept_slot_invalid"
        slots[key] = {
            "key": key,
            "stable_name": stable_name,
            "parent_id": parent_id,
            "target_kind": target_kind,
            "allowed_decisions": allowed_decisions,
            "allowed_existing_concept_ids": allowed_existing_ids,
            "allow_unreferenced": bool(raw_slot.get("allow_unreferenced")),
        }

    fixed_ids = {
        _text(item)
        for item in _sequence(
            guard_contract.get("fixed_authorised_concept_ids")
        )
        if _text(item)
    }
    rules: list[dict[str, Any]] = []
    rule_ids: set[str] = set()
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, Mapping):
            return None, "kr_materialisation_guard_relationship_rule_invalid"
        rule_id = _text(raw_rule.get("rule_id"))
        predicate = _text(raw_rule.get("predicate"))
        source_keys = {
            _text(item)
            for item in _sequence(raw_rule.get("source_slot_keys"))
            if _text(item)
        }
        target_keys = {
            _text(item)
            for item in _sequence(raw_rule.get("target_slot_keys"))
            if _text(item)
        }
        source_ids = {
            _text(item)
            for item in _sequence(raw_rule.get("source_fixed_ids"))
            if _text(item)
        }
        target_ids = {
            _text(item)
            for item in _sequence(raw_rule.get("target_fixed_ids"))
            if _text(item)
        }
        try:
            minimum_count = int(raw_rule.get("minimum_count", 1))
            maximum_count = int(raw_rule.get("maximum_count", 1))
        except (TypeError, ValueError):
            return None, "kr_materialisation_guard_relationship_rule_invalid"
        if (
            not rule_id
            or rule_id in rule_ids
            or not predicate
            or not (source_keys or source_ids)
            or not (target_keys or target_ids)
            or not source_keys.issubset(slots)
            or not target_keys.issubset(slots)
            or not source_ids.issubset(fixed_ids)
            or not target_ids.issubset(fixed_ids)
            or minimum_count < 0
            or maximum_count < minimum_count
            or maximum_count > 256
        ):
            return None, "kr_materialisation_guard_relationship_rule_invalid"
        rule_ids.add(rule_id)
        rules.append(
            {
                "rule_id": rule_id,
                "predicate": predicate,
                "source_slot_keys": source_keys,
                "target_slot_keys": target_keys,
                "source_fixed_ids": source_ids,
                "target_fixed_ids": target_ids,
                "minimum_count": minimum_count,
                "maximum_count": maximum_count,
            }
        )

    try:
        max_concepts = int(
            guard_contract.get("max_concept_specs", len(slots))
        )
        max_relationships = int(
            guard_contract.get("max_relationship_specs", sum(
                rule["maximum_count"] for rule in rules
            ))
        )
    except (TypeError, ValueError):
        return None, "kr_materialisation_guard_contract_bounds_invalid"
    if (
        max_concepts != len(slots)
        or max_concepts < 1
        or max_concepts > _MAX_GUARD_CONCEPT_SLOTS
        or max_relationships < 0
        or max_relationships > _MAX_GUARD_RELATIONSHIP_RULES
    ):
        return None, "kr_materialisation_guard_contract_bounds_invalid"

    return {
        "slots": slots,
        "rules": rules,
        "fixed_ids": fixed_ids,
        "max_concepts": max_concepts,
        "max_relationships": max_relationships,
        "require_all_slots": guard_contract.get("require_all_concept_slots")
        is not False,
        "reject_unreferenced": guard_contract.get(
            "reject_unreferenced_concepts"
        )
        is not False,
        "require_fixed_ids_referenced": {
            _text(item)
            for item in _sequence(
                guard_contract.get("require_fixed_ids_referenced")
            )
            if _text(item)
        },
    }, None


def _concept_spec_key(spec: Mapping[str, Any]) -> str:
    return _text(
        spec.get("key")
        or spec.get("target_key")
        or spec.get("target_name")
        or spec.get("name")
    )


def _validate_concept_specs(
    *,
    contract: Mapping[str, Any],
    concept_specs: Any,
) -> tuple[dict[str, Mapping[str, Any]] | None, str | None]:
    raw_specs = _sequence(concept_specs)
    slots = contract["slots"]
    if (
        len(raw_specs) > int(contract["max_concepts"])
        or (
            bool(contract["require_all_slots"])
            and len(raw_specs) != len(slots)
        )
    ):
        return None, "kr_materialisation_guard_concept_count_rejected"

    specs: dict[str, Mapping[str, Any]] = {}
    for raw_spec in raw_specs:
        if not isinstance(raw_spec, Mapping):
            return None, "kr_materialisation_guard_concept_spec_invalid"
        key = _concept_spec_key(raw_spec)
        slot = slots.get(key)
        if not key or slot is None or key in specs:
            return None, "kr_materialisation_guard_concept_slot_rejected"
        decision = _text(raw_spec.get("decision")).lower()
        target_name = _text(raw_spec.get("target_name") or raw_spec.get("name"))
        target_kind = _text(
            raw_spec.get("target_kind")
            or raw_spec.get("kind")
            or raw_spec.get("kind_hint")
        ).lower()
        parent_id = _text(raw_spec.get("parent_id"))
        description = _text(
            raw_spec.get("description_text") or raw_spec.get("description")
        )
        raw_create_specs = raw_spec.get("concepts")
        create_specs = _sequence(raw_create_specs)
        if (
            decision not in slot["allowed_decisions"]
            or target_name != slot["stable_name"]
            or target_kind != slot["target_kind"]
            or parent_id != slot["parent_id"]
            or not description
            or len(description) > _MAX_DESCRIPTION_CHARS
        ):
            return None, "kr_materialisation_guard_concept_spec_rejected"
        if decision == "create":
            if len(create_specs) != 1 or not isinstance(create_specs[0], Mapping):
                return None, "kr_materialisation_guard_create_payload_rejected"
            create_spec = create_specs[0]
            if (
                _text(create_spec.get("name")) != slot["stable_name"]
                or _text(create_spec.get("kind")).lower() != slot["target_kind"]
                or len(_text(create_spec.get("description")))
                > _MAX_DESCRIPTION_CHARS
                or _text(
                    raw_spec.get("existing_concept_id")
                    or raw_spec.get("concept_id")
                )
            ):
                return None, "kr_materialisation_guard_create_payload_rejected"
        else:
            existing_concept_id = _text(
                raw_spec.get("existing_concept_id") or raw_spec.get("concept_id")
            )
            if (
                existing_concept_id == ""
                or existing_concept_id
                not in slot["allowed_existing_concept_ids"]
                or raw_create_specs not in (None, [])
            ):
                return None, "kr_materialisation_guard_reuse_payload_rejected"
        specs[key] = raw_spec

    if bool(contract["require_all_slots"]) and set(specs) != set(slots):
        return None, "kr_materialisation_guard_concept_slots_incomplete"
    return specs, None


def _relationship_reference(
    spec: Mapping[str, Any],
    *,
    side: str,
    slot_keys: set[str],
    fixed_ids: set[str],
) -> tuple[tuple[str, str] | None, str | None]:
    key = _text(spec.get(f"{side}_key"))
    concept_id = _text(spec.get(f"{side}_id"))
    if bool(key) == bool(concept_id):
        return None, "kr_materialisation_guard_relationship_endpoint_invalid"
    if key:
        if key not in slot_keys:
            return None, "kr_materialisation_guard_relationship_endpoint_rejected"
        return ("slot", key), None
    if concept_id not in fixed_ids:
        return None, "kr_materialisation_guard_relationship_endpoint_rejected"
    return ("fixed", concept_id), None


def _normalise_relationship_predicate(
    spec: Mapping[str, Any],
    *,
    invalid_code: str,
    apply_alias: bool = False,
) -> tuple[str | None, str | None]:
    """Canonicalise the model-facing ``predicate_id`` alias before execution."""

    predicate = _text(spec.get("predicate"))
    predicate_id = _text(spec.get("predicate_id"))
    if predicate and predicate_id and predicate != predicate_id:
        return None, invalid_code
    if predicate:
        return predicate, None
    if not predicate_id:
        return "", None
    if not isinstance(spec, MutableMapping):
        return None, invalid_code
    if apply_alias:
        spec["predicate"] = predicate_id
    return predicate_id, None


def _matching_rule_ids(
    *,
    contract: Mapping[str, Any],
    predicate: str,
    source: tuple[str, str],
    target: tuple[str, str],
) -> list[str]:
    matches: list[str] = []
    for rule in contract["rules"]:
        if predicate != rule["predicate"]:
            continue
        source_values = (
            rule["source_slot_keys"]
            if source[0] == "slot"
            else rule["source_fixed_ids"]
        )
        target_values = (
            rule["target_slot_keys"]
            if target[0] == "slot"
            else rule["target_fixed_ids"]
        )
        if source[1] in source_values and target[1] in target_values:
            matches.append(rule["rule_id"])
    return matches


def _validate_relationship_specs(
    *,
    contract: Mapping[str, Any],
    relationship_specs: Any,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    raw_specs = _sequence(relationship_specs)
    if len(raw_specs) > int(contract["max_relationships"]):
        return None, "kr_materialisation_guard_relationship_count_rejected"
    slot_keys = set(contract["slots"])
    fixed_ids = set(contract["fixed_ids"])
    normalised: list[dict[str, Any]] = []
    rule_counts: Counter[str] = Counter()
    referenced_slots: set[str] = set()
    referenced_fixed_ids: set[str] = set()
    seen: set[tuple[tuple[str, str], str, tuple[str, str]]] = set()
    for raw_spec in raw_specs:
        if not isinstance(raw_spec, Mapping):
            return None, "kr_materialisation_guard_relationship_spec_invalid"
        source, source_error = _relationship_reference(
            raw_spec, side="source", slot_keys=slot_keys, fixed_ids=fixed_ids
        )
        target, target_error = _relationship_reference(
            raw_spec, side="target", slot_keys=slot_keys, fixed_ids=fixed_ids
        )
        if source_error or target_error or source is None or target is None:
            return None, source_error or target_error
        predicate, predicate_error = _normalise_relationship_predicate(
            raw_spec,
            invalid_code="kr_materialisation_guard_relationship_spec_invalid",
        )
        if predicate_error is not None or predicate is None:
            return None, predicate_error
        matches = _matching_rule_ids(
            contract=contract,
            predicate=predicate,
            source=source,
            target=target,
        )
        identity = (source, predicate, target)
        if not matches or identity in seen:
            return None, "kr_materialisation_guard_relationship_rejected"
        selected_rule = sorted(matches)[0]
        rule_counts[selected_rule] += 1
        seen.add(identity)
        if source[0] == "slot":
            referenced_slots.add(source[1])
        else:
            referenced_fixed_ids.add(source[1])
        if target[0] == "slot":
            referenced_slots.add(target[1])
        else:
            referenced_fixed_ids.add(target[1])
        normalised.append(
            {
                "source": source,
                "predicate": predicate,
                "target": target,
                "rule_id": selected_rule,
            }
        )

    for rule in contract["rules"]:
        count = rule_counts[rule["rule_id"]]
        if count < rule["minimum_count"] or count > rule["maximum_count"]:
            return None, "kr_materialisation_guard_relationship_rule_count_rejected"
    if bool(contract["reject_unreferenced"]):
        required_slots = {
            key
            for key, slot in contract["slots"].items()
            if not bool(slot["allow_unreferenced"])
        }
        if not required_slots.issubset(referenced_slots):
            return None, "kr_materialisation_guard_unreferenced_concept_rejected"
    if not set(contract["require_fixed_ids_referenced"]).issubset(
        referenced_fixed_ids
    ):
        return None, "kr_materialisation_guard_fixed_endpoint_missing"
    for raw_spec in raw_specs:
        if not isinstance(raw_spec, Mapping):
            continue
        _normalise_relationship_predicate(
            raw_spec,
            invalid_code="kr_materialisation_guard_relationship_spec_invalid",
            apply_alias=True,
        )
    return normalised, None


def _verified_slot_concept_ids(
    *,
    contract: Mapping[str, Any],
    concept_iteration_results: Any,
) -> tuple[dict[str, str] | None, str | None]:
    results = _sequence(concept_iteration_results)
    verified: dict[str, str] = {}
    for row in results:
        if not isinstance(row, Mapping) or row.get("completed") is not True:
            return None, "kr_materialisation_guard_concept_result_unverified"
        item = row.get("item")
        result = row.get("result")
        if not isinstance(item, Mapping) or not isinstance(result, Mapping):
            return None, "kr_materialisation_guard_concept_result_unverified"
        key = _concept_spec_key(item)
        concept_id = _text(result.get("kr_concept_id"))
        readback_id = _text(result.get("kr_readback_concept_id"))
        parent_id = _text(result.get("kr_concept_parent_id"))
        expected_existing_id = _text(
            item.get("existing_concept_id") or item.get("concept_id")
        )
        if (
            key not in contract["slots"]
            or key in verified
            or not concept_id
            or concept_id != readback_id
            or parent_id != contract["slots"][key]["parent_id"]
            or (expected_existing_id and concept_id != expected_existing_id)
        ):
            return None, "kr_materialisation_guard_concept_result_unverified"
        verified[key] = concept_id
    if set(verified) != set(contract["slots"]):
        return None, "kr_materialisation_guard_concept_results_incomplete"
    return verified, None


def validate_kr_materialisation_guard(
    *,
    guard_contract: Mapping[str, Any] | None,
    phase: str,
    concept_specs: Any = None,
    relationship_specs: Any = None,
    concept_iteration_results: Any = None,
    resolved_relationship_specs: Any = None,
) -> dict[str, Any]:
    """Validate optional caller policy before materialisation side effects."""

    phase_text = _text(phase).lower()
    if not isinstance(guard_contract, Mapping):
        return _pass(phase=phase_text or "plan", guard_applied=False)
    if phase_text not in {"plan", "resolved_relationships"}:
        return _reject(
            "kr_materialisation_guard_phase_invalid", phase=phase_text or "unknown"
        )
    contract, contract_error = _normalise_contract(guard_contract)
    if contract is None:
        return _reject(
            contract_error or "kr_materialisation_guard_contract_invalid",
            phase=phase_text,
        )
    concept_by_key, concept_error = _validate_concept_specs(
        contract=contract,
        concept_specs=concept_specs,
    )
    if concept_by_key is None:
        return _reject(
            concept_error or "kr_materialisation_guard_concepts_rejected",
            phase=phase_text,
        )
    requested, relationship_error = _validate_relationship_specs(
        contract=contract,
        relationship_specs=relationship_specs,
    )
    if requested is None:
        return _reject(
            relationship_error
            or "kr_materialisation_guard_relationships_rejected",
            phase=phase_text,
        )
    if phase_text == "plan":
        return _pass(
            phase=phase_text,
            guard_applied=True,
            concept_count=len(concept_by_key),
            relationship_count=len(requested),
        )

    verified, verified_error = _verified_slot_concept_ids(
        contract=contract,
        concept_iteration_results=concept_iteration_results,
    )
    if verified is None:
        return _reject(
            verified_error or "kr_materialisation_guard_results_rejected",
            phase=phase_text,
        )
    expected: set[tuple[str, str, str]] = set()
    for spec in requested:
        source = (
            verified[spec["source"][1]]
            if spec["source"][0] == "slot"
            else spec["source"][1]
        )
        target = (
            verified[spec["target"][1]]
            if spec["target"][0] == "slot"
            else spec["target"][1]
        )
        expected.add((source, spec["predicate"], target))

    observed: set[tuple[str, str, str]] = set()
    allowed_ids = set(verified.values()) | set(contract["fixed_ids"])
    raw_resolved = _sequence(resolved_relationship_specs)
    if len(raw_resolved) != len(expected):
        return _reject(
            "kr_materialisation_guard_resolved_relationship_count_rejected",
            phase=phase_text,
        )
    for raw_spec in raw_resolved:
        if not isinstance(raw_spec, Mapping):
            return _reject(
                "kr_materialisation_guard_resolved_relationship_invalid",
                phase=phase_text,
            )
        source_id = _text(raw_spec.get("source_id"))
        predicate, predicate_error = _normalise_relationship_predicate(
            raw_spec,
            invalid_code=(
                "kr_materialisation_guard_resolved_relationship_invalid"
            ),
        )
        if predicate_error is not None or predicate is None:
            return _reject(
                predicate_error
                or "kr_materialisation_guard_resolved_relationship_invalid",
                phase=phase_text,
            )
        target_id = _text(raw_spec.get("target_id"))
        triple = (source_id, predicate, target_id)
        if (
            source_id not in allowed_ids
            or target_id not in allowed_ids
            or triple in observed
        ):
            return _reject(
                "kr_materialisation_guard_resolved_endpoint_rejected",
                phase=phase_text,
            )
        observed.add(triple)
    if observed != expected:
        return _reject(
            "kr_materialisation_guard_resolved_relationship_rejected",
            phase=phase_text,
        )
    for raw_spec in raw_resolved:
        if not isinstance(raw_spec, Mapping):
            continue
        _normalise_relationship_predicate(
            raw_spec,
            invalid_code="kr_materialisation_guard_resolved_relationship_invalid",
            apply_alias=True,
        )
    return _pass(
        phase=phase_text,
        guard_applied=True,
        concept_count=len(concept_by_key),
        relationship_count=len(observed),
    )


__all__ = [
    "KR_MATERIALISATION_GUARD_SCHEMA_VERSION",
    "validate_kr_materialisation_guard",
]
