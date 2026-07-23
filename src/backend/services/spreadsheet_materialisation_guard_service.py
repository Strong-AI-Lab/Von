"""Compile spreadsheet record contracts into generic KR write guards."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import hashlib
import json
from typing import Any

from ..utils.concept_id_utils import canonicalise_vontology_concept_id


_MAX_KR_CONCEPT_SPECS = 24
_MAX_KR_RELATIONSHIP_SPECS = 40


def _text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _rule_id(value: Any) -> str:
    return f"rule_{_digest(value)[:24]}"


def _row_identity(
    row: Mapping[str, Any], *, identity_columns: Sequence[str]
) -> dict[str, Any]:
    """Return a reorder-stable projection with no physical row coordinates."""

    fields_by_column: dict[str, dict[str, Any]] = {}
    raw_fields = row.get("fields")
    if isinstance(raw_fields, Sequence) and not isinstance(
        raw_fields, (str, bytes, bytearray)
    ):
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping):
                continue
            column = _text(raw_field.get("column"))
            if column not in identity_columns:
                continue
            fields_by_column[column] = {
                key: (
                    _normalise_identity_value(raw_field.get(key))
                    if key in {"value", "cached_value"}
                    else raw_field.get(key)
                )
                for key in (
                    "column",
                    "value",
                    "value_type",
                    "formula",
                    "cached_value",
                )
                if key in raw_field
            }
    fields = [
        fields_by_column[column]
        for column in identity_columns
        if column in fields_by_column
    ]
    return {"sheet": _text(row.get("sheet")), "fields": fields}


def _normalise_identity_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, (bool, int, float)):
        return value
    return _stable_json(value).casefold()


def _entity_identity(
    row: Mapping[str, Any], *, identity_columns: Sequence[str]
) -> list[tuple[str, Any]] | None:
    """Return a coordinate-free identity projection for a declared entity.

    Values are normalised only inside the actor-scoped logical dataset. They
    are never used to search for or authorise reuse of an arbitrary existing
    Vontology concept.
    """

    values_by_column: dict[str, Any] = {}
    raw_fields = row.get("fields")
    if isinstance(raw_fields, Sequence) and not isinstance(
        raw_fields, (str, bytes, bytearray)
    ):
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping):
                continue
            column = _text(raw_field.get("column"))
            if column in identity_columns:
                values_by_column[column] = _normalise_identity_value(
                    raw_field.get("value")
                )
    if any(column not in values_by_column for column in identity_columns):
        return None
    if any(values_by_column[column] in (None, "") for column in identity_columns):
        return None
    return [(column, values_by_column[column]) for column in identity_columns]


def _slot(
    *,
    record_id: str,
    role: str,
    parent_id: str,
    identity: Any,
    stable_name: str | None = None,
    identity_namespace: str | None = None,
    reusable_existing_concept_ids: set[str] | None = None,
) -> dict[str, Any]:
    namespace = identity_namespace or record_id
    token = _digest(
        {
            "identity_namespace": namespace,
            "role": role,
            "parent_id": parent_id,
            "identity": identity,
        }
    )
    resolved_name = stable_name or f"{namespace}-{role}-{token[:16]}"
    stable_concept_id = canonicalise_vontology_concept_id(resolved_name)
    if reusable_existing_concept_ids is None:
        allowed_decisions = ["create", "reuse_existing"]
        allowed_existing_concept_ids = (
            [stable_concept_id] if stable_concept_id else []
        )
    elif stable_concept_id and stable_concept_id in reusable_existing_concept_ids:
        allowed_decisions = ["reuse_existing"]
        allowed_existing_concept_ids = [stable_concept_id]
    else:
        allowed_decisions = ["create"]
        allowed_existing_concept_ids = []
    return {
        "key": f"slot_{token[:24]}",
        "stable_name": resolved_name,
        "target_kind": "instance",
        "parent_id": parent_id,
        "allowed_decisions": allowed_decisions,
        "allowed_existing_concept_ids": allowed_existing_concept_ids,
        "allow_unreferenced": False,
    }


def build_spreadsheet_kr_materialisation_guard(
    *,
    record: Mapping[str, Any],
    reusable_existing_concept_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Derive the exact additive-write envelope for one compiled record.

    When ``reusable_existing_concept_ids`` is provided, each slot is bound to
    exactly one current-state decision: create when its deterministic concept
    ID is absent, or reuse when canonical read-back confirms that ID exists.
    ``None`` preserves the unbound authority template used by planning and
    guard-shape tests.
    """

    reusable_ids = (
        {
            _text(concept_id)
            for concept_id in reusable_existing_concept_ids
            if _text(concept_id)
        }
        if reusable_existing_concept_ids is not None
        else None
    )

    record_id = _text(record.get("source_record_id"))
    version_id = _text(record.get("source_record_version_id"))
    readback = record.get("representation_readback_contract")
    authority = record.get("write_authority_contract")
    if (
        not record_id
        or not version_id
        or not isinstance(readback, Mapping)
        or not isinstance(authority, Mapping)
    ):
        return {
            "success": False,
            "error_code": "spreadsheet_materialisation_guard_input_missing",
        }

    source_parent_ids = authority.get("source_identity_parent_ids")
    allowed_parent_ids = {
        _text(item)
        for item in (authority.get("allowed_concept_parent_ids") or [])
        if _text(item)
    }
    allowed_predicates = {
        _text(item)
        for item in (authority.get("allowed_relationship_predicate_ids") or [])
        if _text(item)
    }
    if (
        not isinstance(source_parent_ids, Mapping)
        or not allowed_parent_ids
        or not allowed_predicates
    ):
        return {
            "success": False,
            "error_code": "spreadsheet_materialisation_guard_authority_invalid",
        }
    source_record_parent = _text(source_parent_ids.get("source_record"))
    source_version_parent = _text(
        source_parent_ids.get("source_record_version")
    )
    if (
        source_record_parent not in allowed_parent_ids
        or source_version_parent not in allowed_parent_ids
    ):
        return {
            "success": False,
            "error_code": "spreadsheet_materialisation_guard_authority_invalid",
        }

    slots: list[dict[str, Any]] = [
        _slot(
            record_id=record_id,
            role="source-record",
            parent_id=source_record_parent,
            identity="source-record",
            stable_name=record_id,
            reusable_existing_concept_ids=reusable_ids,
        ),
        _slot(
            record_id=record_id,
            role="source-record-version",
            parent_id=source_version_parent,
            identity=record.get("record_fingerprint"),
            stable_name=version_id,
            reusable_existing_concept_ids=reusable_ids,
        ),
    ]
    stable_identity_slots = {
        "source_record": slots[0]["key"],
        "source_record_version": slots[1]["key"],
    }
    slots_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    slots_by_type[source_record_parent].append(slots[0])
    slots_by_type[source_version_parent].append(slots[1])
    rules: list[dict[str, Any]] = []
    source_groups = (
        record.get("source_groups")
        if isinstance(record.get("source_groups"), Mapping)
        else {}
    )
    logical_dataset_id = _text(record.get("logical_dataset_id"))
    slots_by_key = {str(slot["key"]): slot for slot in slots}

    def add_slot(
        *,
        role: str,
        parent_id: str,
        identity: Any,
        identity_namespace: str | None = None,
    ) -> dict[str, Any]:
        if parent_id not in allowed_parent_ids:
            raise ValueError("spreadsheet_materialisation_guard_parent_not_authorised")
        created = _slot(
            record_id=record_id,
            role=role,
            parent_id=parent_id,
            identity=identity,
            identity_namespace=identity_namespace,
            reusable_existing_concept_ids=reusable_ids,
        )
        existing = slots_by_key.get(str(created["key"]))
        if existing is not None:
            return existing
        slots.append(created)
        slots_by_key[str(created["key"])] = created
        slots_by_type[parent_id].append(created)
        return created

    def endpoint_for_type(
        *, concept_type_id: str, identity: Any
    ) -> dict[str, Any]:
        candidates = slots_by_type.get(concept_type_id) or []
        if len(candidates) == 1:
            return candidates[0]
        return add_slot(
            role="related-endpoint",
            parent_id=concept_type_id,
            identity=identity,
        )

    def add_exact_rule(
        *, predicate: str, source_key: str, target_key: str
    ) -> None:
        if predicate not in allowed_predicates:
            raise ValueError(
                "spreadsheet_materialisation_guard_predicate_not_authorised"
            )
        rules.append(
            {
                "rule_id": _rule_id(
                    {
                        "source": source_key,
                        "predicate": predicate,
                        "target": target_key,
                    }
                ),
                "predicate": predicate,
                "source_slot_keys": [source_key],
                "target_slot_keys": [target_key],
                "minimum_count": 1,
                "maximum_count": 1,
            }
        )

    used_predicates: set[str] = set()
    raw_group_contracts = readback.get("source_group_contracts")
    if not isinstance(raw_group_contracts, Sequence) or isinstance(
        raw_group_contracts, (str, bytes, bytearray)
    ):
        raw_group_contracts = []
    try:
        for raw_contract in raw_group_contracts:
            if not isinstance(raw_contract, Mapping):
                continue
            group_key = _text(raw_contract.get("source_group_key"))
            artefact_type = _text(raw_contract.get("required_concept_type_id"))
            record_predicate = _text(raw_contract.get("record_link_predicate_id"))
            other_type = _text(
                raw_contract.get("record_link_other_concept_type_id")
            )
            artefact_argument = _text(
                raw_contract.get("record_link_artefact_argument")
            ).lower()
            identity_columns = [
                _text(item)
                for item in (raw_contract.get("identity_columns") or [])
                if _text(item)
            ]
            rows = source_groups.get(group_key)
            if (
                not group_key
                or artefact_type not in allowed_parent_ids
                or record_predicate not in allowed_predicates
                or other_type not in allowed_parent_ids
                or artefact_argument not in {"source", "target"}
                or not identity_columns
                or not isinstance(rows, list)
            ):
                return {
                    "success": False,
                    "error_code": "spreadsheet_materialisation_guard_contract_invalid",
                }

            occurrences: Counter[str] = Counter()
            for raw_row in rows:
                if not isinstance(raw_row, Mapping):
                    continue
                row_identity = (
                    {"group": "root", "source_record_id": record_id}
                    if group_key == "root"
                    else _row_identity(
                        raw_row,
                        identity_columns=identity_columns,
                    )
                )
                row_token = _digest(row_identity)
                occurrence = occurrences[row_token]
                occurrences[row_token] += 1
                artefact = add_slot(
                    role=f"{group_key}-artefact",
                    parent_id=artefact_type,
                    identity={
                        "group": group_key,
                        "row": row_identity,
                        "occurrence": occurrence,
                    },
                )
                other = endpoint_for_type(
                    concept_type_id=other_type,
                    identity={
                        "group": group_key,
                        "relationship": "record-link",
                        "artefact": artefact["key"],
                    },
                )
                source = artefact if artefact_argument == "source" else other
                target = other if artefact_argument == "source" else artefact
                add_exact_rule(
                    predicate=record_predicate,
                    source_key=str(source["key"]),
                    target_key=str(target["key"]),
                )
                used_predicates.add(record_predicate)

                raw_children = raw_contract.get(
                    "required_per_artefact_relationships"
                )
                if not isinstance(raw_children, Sequence) or isinstance(
                    raw_children, (str, bytes, bytearray)
                ):
                    raw_children = []
                for child_index, raw_child in enumerate(raw_children):
                    if not isinstance(raw_child, Mapping):
                        continue
                    predicate = _text(raw_child.get("predicate_id"))
                    other_child_type = _text(
                        raw_child.get("other_concept_type_id")
                    )
                    child_argument = _text(
                        raw_child.get("artefact_argument")
                    ).lower()
                    raw_endpoint_identity = raw_child.get(
                        "other_endpoint_identity"
                    )
                    endpoint_scope = "source_record"
                    endpoint_identity_columns: list[str] = []
                    if isinstance(raw_endpoint_identity, Mapping):
                        endpoint_scope = _text(
                            raw_endpoint_identity.get("scope")
                        ).lower()
                        endpoint_identity_columns = [
                            _text(item)
                            for item in (
                                raw_endpoint_identity.get("identity_columns")
                                or []
                            )
                            if _text(item)
                        ]
                    if (
                        predicate not in allowed_predicates
                        or other_child_type not in allowed_parent_ids
                        or child_argument not in {"source", "target"}
                        or endpoint_scope
                        not in {"source_record", "logical_dataset"}
                        or (
                            endpoint_scope == "logical_dataset"
                            and (
                                not logical_dataset_id
                                or not endpoint_identity_columns
                            )
                        )
                    ):
                        return {
                            "success": False,
                            "error_code": (
                                "spreadsheet_materialisation_guard_contract_invalid"
                            ),
                        }
                    if endpoint_scope == "logical_dataset":
                        declared_identity = _entity_identity(
                            raw_row,
                            identity_columns=endpoint_identity_columns,
                        )
                        if declared_identity is None:
                            return {
                                "success": False,
                                "error_code": (
                                    "spreadsheet_materialisation_guard_"
                                    "entity_identity_missing"
                                ),
                            }
                        child = add_slot(
                            role=(
                                f"{_text(raw_contract.get('semantic_role'))}"
                                "-related"
                            ),
                            parent_id=other_child_type,
                            identity={
                                "scope": "logical_dataset",
                                "predicate": predicate,
                                "columns": declared_identity,
                            },
                            identity_namespace=logical_dataset_id,
                        )
                    else:
                        child = add_slot(
                            role=f"{group_key}-related",
                            parent_id=other_child_type,
                            identity={
                                "artefact": artefact["key"],
                                "predicate": predicate,
                                "index": child_index,
                            },
                        )
                    source = artefact if child_argument == "source" else child
                    target = child if child_argument == "source" else artefact
                    add_exact_rule(
                        predicate=predicate,
                        source_key=str(source["key"]),
                        target_key=str(target["key"]),
                    )
                    used_predicates.add(predicate)
    except ValueError:
        return {
            "success": False,
            "error_code": "spreadsheet_materialisation_guard_authority_exceeded",
        }

    raw_type_minimums = readback.get("required_concept_type_minimums")
    if not isinstance(raw_type_minimums, Sequence) or isinstance(
        raw_type_minimums, (str, bytes, bytearray)
    ):
        raw_type_minimums = []
    for raw_minimum in raw_type_minimums:
        if not isinstance(raw_minimum, Mapping):
            continue
        concept_type = _text(raw_minimum.get("concept_type_id"))
        try:
            minimum = max(0, int(raw_minimum.get("minimum_count", 0)))
        except (TypeError, ValueError):
            minimum = 0
        if concept_type and concept_type not in allowed_parent_ids:
            return {
                "success": False,
                "error_code": "spreadsheet_materialisation_guard_authority_exceeded",
            }
        while concept_type and len(slots_by_type[concept_type]) < minimum:
            add_slot(
                role="required-type",
                parent_id=concept_type,
                identity={
                    "concept_type": concept_type,
                    "ordinal": len(slots_by_type[concept_type]),
                },
            )

    fixed_ids = [_text(record.get("file_copy_concept_id"))]
    fixed_ids = [item for item in fixed_ids if item]
    all_slot_keys = [str(item["key"]) for item in slots]
    role_references: dict[str, tuple[str, str]] = {
        "source_record": ("slot", str(slots[0]["key"])),
        "source_record_version": ("slot", str(slots[1]["key"])),
    }
    if fixed_ids:
        role_references["source_file_copy"] = ("fixed", fixed_ids[0])
    structural_predicates: set[str] = set()
    raw_structural_rules = authority.get("structural_relationship_rules")
    if not isinstance(raw_structural_rules, Sequence) or isinstance(
        raw_structural_rules, (str, bytes, bytearray)
    ):
        raw_structural_rules = []
    for raw_rule in raw_structural_rules:
        if not isinstance(raw_rule, Mapping):
            continue
        predicate = _text(raw_rule.get("predicate"))
        source = role_references.get(_text(raw_rule.get("source_role")))
        target = role_references.get(_text(raw_rule.get("target_role")))
        if (
            predicate not in allowed_predicates
            or source is None
            or target is None
        ):
            return {
                "success": False,
                "error_code": "spreadsheet_materialisation_guard_authority_invalid",
            }
        rule: dict[str, Any] = {
            "rule_id": _rule_id(
                {
                    "source": source,
                    "predicate": predicate,
                    "target": target,
                }
            ),
            "predicate": predicate,
            "minimum_count": 1,
            "maximum_count": 1,
        }
        rule[f"source_{'slot_keys' if source[0] == 'slot' else 'fixed_ids'}"] = [
            source[1]
        ]
        rule[f"target_{'slot_keys' if target[0] == 'slot' else 'fixed_ids'}"] = [
            target[1]
        ]
        rules.append(rule)
        structural_predicates.add(predicate)
    raw_predicate_minimums = readback.get(
        "required_relationship_predicate_minimums"
    )
    if not isinstance(raw_predicate_minimums, Sequence) or isinstance(
        raw_predicate_minimums, (str, bytes, bytearray)
    ):
        raw_predicate_minimums = []
    for raw_minimum in raw_predicate_minimums:
        if not isinstance(raw_minimum, Mapping):
            continue
        predicate = _text(raw_minimum.get("predicate_id"))
        if (
            not predicate
            or predicate not in allowed_predicates
            or predicate in used_predicates
            or predicate in structural_predicates
        ):
            continue
        try:
            minimum = max(0, int(raw_minimum.get("minimum_count", 0)))
        except (TypeError, ValueError):
            minimum = 0
        if minimum == 0:
            continue
        rules.append(
            {
                "rule_id": _rule_id(
                    {
                        "predicate": predicate,
                        "scope": "grounded-record-endpoints",
                    }
                ),
                "predicate": predicate,
                "source_slot_keys": all_slot_keys,
                "target_slot_keys": all_slot_keys,
                "source_fixed_ids": fixed_ids,
                "target_fixed_ids": fixed_ids,
                "minimum_count": minimum,
                "maximum_count": minimum,
            }
        )

    if (
        len(slots) > _MAX_KR_CONCEPT_SPECS
        or len(rules) > _MAX_KR_RELATIONSHIP_SPECS
    ):
        return {
            "success": False,
            "error_code": "spreadsheet_materialisation_guard_bounds_exceeded",
            "concept_spec_count": len(slots),
            "relationship_spec_count": len(rules),
        }

    authority = {
        "record_processing_fingerprint": record.get(
            "record_processing_fingerprint"
        ),
        "slots": slots,
        "rules": rules,
        "fixed_ids": fixed_ids,
    }
    guard = {
        "schema_version": "kr_materialisation_guard.v1",
        "guard_id": f"sha256:{_digest(authority)}",
        "max_concept_specs": len(slots),
        "max_relationship_specs": len(rules),
        "require_all_concept_slots": True,
        "reject_unreferenced_concepts": True,
        "concept_slots": slots,
        "relationship_rules": rules,
        "fixed_authorised_concept_ids": fixed_ids,
        "require_fixed_ids_referenced": fixed_ids,
        "stable_identity_slots": stable_identity_slots,
    }
    return {
        "success": True,
        "schema_version": "spreadsheet_kr_materialisation_guard.v1",
        "materialisation_guard": guard,
    }


__all__ = ["build_spreadsheet_kr_materialisation_guard"]
