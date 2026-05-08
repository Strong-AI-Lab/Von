"""Runtime projection of tool payloads from Vontology evidence contracts.

The projection rules are read from represented tool, field, payload-path, and
evidence-view concepts. This module is deliberately generic: tool-specific
behaviour belongs in Vontology materialisation services, not in the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from .workflow_vontology_materialisation_helpers import (
    load_concept,
    normalise_relationship_targets,
)

VIEW_PURPOSE_PRIORITY: tuple[str, ...] = (
    "#V#tool_evidence_view_purpose_final_answer",
    "#V#tool_evidence_view_purpose_user_display",
    "#V#tool_evidence_view_purpose_follow_up_selection",
    "#V#tool_evidence_view_purpose_telemetry",
)

PROJECTION_SCHEMA_VERSION = "tool_evidence_projection.v1"


@dataclass(frozen=True)
class ToolFieldContract:
    concept_id: str
    output_key: str
    wire_aliases: tuple[str, ...]
    payload_paths: tuple[str, ...]
    required: bool
    included: bool
    redacted: bool


@dataclass(frozen=True)
class ToolProjectionContract:
    tool_concept_id: str
    evidence_view_concept_ids: tuple[str, ...]
    fields: tuple[ToolFieldContract, ...]
    output_field_ids: tuple[str, ...]
    collection_field_ids: tuple[str, ...]


def project_tool_payload_for_llm(
    tool_name: str,
    payload: Mapping[str, Any],
    *,
    max_collection_items: int = 40,
) -> dict[str, Any] | None:
    """Return a compact Vontology-backed LLM payload, or None if no contract exists."""

    if not isinstance(tool_name, str) or not tool_name.strip():
        return None
    if not isinstance(payload, Mapping):
        return None

    contract = resolve_tool_projection_contract(tool_name)
    if contract is None or not contract.fields:
        return None

    projected: dict[str, Any] = {
        "_llm_view": PROJECTION_SCHEMA_VERSION,
    }
    telemetry: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "tool_concept_id": contract.tool_concept_id,
        "evidence_view_concept_ids": list(contract.evidence_view_concept_ids),
        "preserved_fields": [],
        "missing_required_fields": [],
        "omitted_fields": [],
        "redacted_fields": [],
    }

    collection_projection = _project_collection_fields(
        contract,
        payload,
        max_collection_items=max_collection_items,
    )
    collection_field_ids = set(contract.collection_field_ids)
    row_projected_field_ids = set(collection_projection.get("field_ids") or [])
    if collection_projection.get("collection_key"):
        collection_key = str(collection_projection["collection_key"])
        projected[collection_key] = collection_projection["items"]
        projected[f"{collection_key}_count"] = collection_projection["total_count"]
        if collection_projection.get("omitted_count"):
            projected[f"{collection_key}_omitted_count"] = collection_projection[
                "omitted_count"
            ]
        telemetry["preserved_fields"].append(
            {
                "field_concept_id": collection_projection["collection_field_id"],
                "output_key": collection_key,
                "item_count": collection_projection["total_count"],
            }
        )
        for field_id in sorted(row_projected_field_ids):
            field = _field_by_id(contract, field_id)
            telemetry["preserved_fields"].append(
                {
                    "field_concept_id": field_id,
                    "output_key": field.output_key if field else field_id,
                    "location": collection_key,
                }
            )

    for field in contract.fields:
        if field.concept_id in collection_field_ids or field.concept_id in row_projected_field_ids:
            continue
        if field.redacted:
            telemetry["redacted_fields"].append(
                {"field_concept_id": field.concept_id, "output_key": field.output_key}
            )
            continue
        found, value = _extract_field_value(payload, field)
        if not found:
            if field.required:
                telemetry["missing_required_fields"].append(
                    {"field_concept_id": field.concept_id, "output_key": field.output_key}
                )
            else:
                telemetry["omitted_fields"].append(
                    {"field_concept_id": field.concept_id, "output_key": field.output_key}
                )
            continue
        compact_value = _compact_value(value)
        if compact_value is None:
            telemetry["omitted_fields"].append(
                {
                    "field_concept_id": field.concept_id,
                    "output_key": field.output_key,
                    "reason": "empty_or_unserialisable_value",
                }
            )
            continue
        projected[field.output_key] = compact_value
        telemetry["preserved_fields"].append(
            {"field_concept_id": field.concept_id, "output_key": field.output_key}
        )

    omitted_output_fields = set(contract.output_field_ids).difference(
        {entry["field_concept_id"] for entry in telemetry["preserved_fields"]}
    )
    omitted_output_fields.difference_update(
        {entry["field_concept_id"] for entry in telemetry["redacted_fields"]}
    )
    omitted_output_fields.difference_update(
        {entry["field_concept_id"] for entry in telemetry["missing_required_fields"]}
    )
    already_omitted = {
        entry["field_concept_id"] for entry in telemetry["omitted_fields"]
    }
    for field_id in sorted(omitted_output_fields.difference(already_omitted)):
        field = _field_by_id(contract, field_id)
        telemetry["omitted_fields"].append(
            {
                "field_concept_id": field_id,
                "output_key": field.output_key if field else field_id,
                "reason": "not_in_selected_evidence_view_or_value_absent",
            }
        )

    projected["_tool_evidence_projection"] = telemetry
    if len(projected) <= 2 and not (
        telemetry["missing_required_fields"] or telemetry["redacted_fields"]
    ):
        return None
    return projected


def resolve_tool_projection_contract(tool_name: str) -> ToolProjectionContract | None:
    """Resolve the represented projection contract for one internal MCP tool name."""

    tool_doc = _find_tool_concept(tool_name)
    if not isinstance(tool_doc, Mapping):
        return None
    tool_concept_id = _clean_str(tool_doc.get("concept_id"))
    if not tool_concept_id:
        return None

    relationships = _relationships(tool_doc)
    output_field_ids = tuple(
        _normalise_unique(relationships.get("#V#tool_has_output_field"))
    )
    preserve_field_ids = tuple(
        _normalise_unique(relationships.get("#V#tool_result_preserves_field"))
    )
    evidence_views = _select_evidence_views(tool_concept_id)
    if not evidence_views and not preserve_field_ids:
        return None

    required_field_ids: set[str] = set()
    included_field_ids: set[str] = set(preserve_field_ids)
    redacted_field_ids: set[str] = set()
    view_ids: list[str] = []
    for view_doc in evidence_views:
        view_id = _clean_str(view_doc.get("concept_id"))
        if view_id:
            view_ids.append(view_id)
        view_relationships = _relationships(view_doc)
        required_field_ids.update(
            _normalise_unique(view_relationships.get("#V#evidence_view_requires_field"))
        )
        included_field_ids.update(
            _normalise_unique(view_relationships.get("#V#evidence_view_includes_field"))
        )
        redacted_field_ids.update(
            _normalise_unique(view_relationships.get("#V#evidence_view_redacts_field"))
        )

    selected_field_ids = _ordered_unique(
        [
            *required_field_ids,
            *included_field_ids,
            *preserve_field_ids,
            *redacted_field_ids,
        ]
    )
    if output_field_ids:
        output_set = set(output_field_ids)
        selected_field_ids = [
            field_id for field_id in selected_field_ids if field_id in output_set
        ]
        if not selected_field_ids and preserve_field_ids:
            selected_field_ids = [
                field_id for field_id in preserve_field_ids if field_id in output_set
            ]

    fields = tuple(
        field
        for field_id in selected_field_ids
        if (
            field := _load_field_contract(
                field_id,
                required=field_id in required_field_ids,
                included=field_id in included_field_ids or field_id in preserve_field_ids,
                redacted=field_id in redacted_field_ids,
            )
        )
        is not None
    )
    collection_field_ids = tuple(
        field.concept_id
        for field in fields
        if "#V#tool_field_role_collection_membership" in _relationship_targets(
            load_concept(field.concept_id),
            "#V#field_has_role",
        )
    )

    return ToolProjectionContract(
        tool_concept_id=tool_concept_id,
        evidence_view_concept_ids=tuple(view_ids),
        fields=fields,
        output_field_ids=output_field_ids,
        collection_field_ids=collection_field_ids,
    )


def _find_tool_concept(tool_name: str) -> Mapping[str, Any] | None:
    cleaned = tool_name.strip()
    for query in (
        {"attributes.mcp_tool_name": cleaned},
        {"attributes.mcp_tool_name": cleaned.lower()},
    ):
        doc = ConceptsRepository.find_one(query)
        if isinstance(doc, Mapping):
            return doc
    return None


def _select_evidence_views(tool_concept_id: str) -> tuple[Mapping[str, Any], ...]:
    candidates = list(
        ConceptsRepository.find(
            {"relationships.#V#evidence_view_applies_to_tool": tool_concept_id},
        )
    )
    if not candidates:
        return ()

    by_priority: dict[int, list[Mapping[str, Any]]] = {}
    fallback: list[Mapping[str, Any]] = []
    for doc in candidates:
        purposes = _relationship_targets(doc, "#V#evidence_view_has_purpose")
        matched = False
        for index, purpose_id in enumerate(VIEW_PURPOSE_PRIORITY):
            if purpose_id in purposes:
                by_priority.setdefault(index, []).append(doc)
                matched = True
        if not matched:
            fallback.append(doc)
    if by_priority:
        return tuple(by_priority[min(by_priority)])
    return tuple(fallback)


def _load_field_contract(
    field_id: str,
    *,
    required: bool,
    included: bool,
    redacted: bool,
) -> ToolFieldContract | None:
    doc = load_concept(field_id)
    if not isinstance(doc, Mapping):
        return None
    raw_attributes = doc.get("attributes")
    attributes: Mapping[str, Any] = raw_attributes if isinstance(raw_attributes, Mapping) else {}
    output_key = _clean_str(attributes.get("field_key")) or _fallback_field_key(field_id)
    aliases = tuple(
        alias
        for alias_id in _relationship_targets(doc, "#V#field_has_wire_alias")
        if (alias := _wire_key_value(alias_id))
    )
    paths = tuple(
        path
        for path_id in _relationship_targets(doc, "#V#field_extracts_from_payload_path")
        if (path := _payload_path_value(path_id))
    )
    if output_key not in aliases:
        aliases = (output_key, *aliases)
    return ToolFieldContract(
        concept_id=field_id,
        output_key=output_key,
        wire_aliases=_dedupe(aliases),
        payload_paths=_dedupe(paths),
        required=required,
        included=included,
        redacted=redacted,
    )


def _project_collection_fields(
    contract: ToolProjectionContract,
    payload: Mapping[str, Any],
    *,
    max_collection_items: int,
) -> dict[str, Any]:
    collection_fields = [
        field for field in contract.fields if field.concept_id in contract.collection_field_ids
    ]
    for collection_field in collection_fields:
        found, value = _extract_field_value(payload, collection_field)
        if not found or not isinstance(value, list):
            continue
        row_fields = [field for field in contract.fields if field.concept_id != collection_field.concept_id]
        rows: list[dict[str, Any]] = []
        projected_field_ids: set[str] = set()
        for item in value[:max_collection_items]:
            if not isinstance(item, Mapping):
                continue
            row: dict[str, Any] = {}
            for field in row_fields:
                found_row, row_value = _extract_field_value(
                    item,
                    _field_for_collection_item(field, collection_field),
                )
                if not found_row:
                    continue
                compact_value = _compact_value(row_value)
                if compact_value is None:
                    continue
                row[field.output_key] = compact_value
                projected_field_ids.add(field.concept_id)
            if row:
                rows.append(row)
        return {
            "collection_field_id": collection_field.concept_id,
            "collection_key": collection_field.output_key,
            "items": rows,
            "total_count": len(value),
            "omitted_count": max(0, len(value) - len(rows)),
            "field_ids": sorted(projected_field_ids),
        }
    return {}


def _field_for_collection_item(
    field: ToolFieldContract,
    collection_field: ToolFieldContract,
) -> ToolFieldContract:
    prefixes = [
        f"{alias}[]." for alias in collection_field.wire_aliases
    ] + [f"{path}[]." for path in collection_field.payload_paths]
    stripped_paths: list[str] = []
    for path in field.payload_paths:
        stripped = path
        for prefix in prefixes:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :]
                break
        stripped_paths.append(stripped)
    return ToolFieldContract(
        concept_id=field.concept_id,
        output_key=field.output_key,
        wire_aliases=field.wire_aliases,
        payload_paths=_dedupe(stripped_paths),
        required=field.required,
        included=field.included,
        redacted=field.redacted,
    )


def _extract_field_value(payload: Mapping[str, Any], field: ToolFieldContract) -> tuple[bool, Any]:
    for alias in field.wire_aliases:
        if alias in payload:
            return True, payload.get(alias)
    for path in field.payload_paths:
        values = _extract_path_values(payload, path)
        values = [value for value in values if value is not None]
        if not values:
            continue
        if len(values) == 1:
            return True, values[0]
        return True, values
    return False, None


def _extract_path_values(value: Any, path: str) -> list[Any]:
    parts = [part for part in str(path or "").split(".") if part]
    if not parts:
        return [value]
    return _extract_path_parts(value, parts)


def _extract_path_parts(value: Any, parts: Sequence[str]) -> list[Any]:
    if not parts:
        return [value]
    part = parts[0]
    rest = parts[1:]
    if part.endswith("[]"):
        key = part[:-2]
        sequence = _mapping_value(value, key)
        if not isinstance(sequence, list):
            return []
        results: list[Any] = []
        for item in sequence:
            results.extend(_extract_path_parts(item, rest))
        return results
    filter_key = None
    filter_value = None
    if "[" in part and part.endswith("]"):
        key, raw_filter = part[:-1].split("[", 1)
        if "=" in raw_filter:
            filter_key, filter_value = raw_filter.split("=", 1)
            part = key
    next_value = _mapping_value(value, part)
    if filter_key and isinstance(next_value, list):
        filtered = [
            item
            for item in next_value
            if isinstance(item, Mapping) and str(item.get(filter_key)) == filter_value
        ]
        results: list[Any] = []
        for item in filtered:
            if rest:
                results.extend(_extract_path_parts(item, rest))
            else:
                results.append(item.get("value") if "value" in item else item)
        return results
    return _extract_path_parts(next_value, rest) if next_value is not None else []


def _mapping_value(value: Any, key: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    if key in value:
        return value.get(key)
    lower_key = key.lower()
    for current_key, current_value in value.items():
        if isinstance(current_key, str) and current_key.lower() == lower_key:
            return current_value
    return None


def _compact_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        compact_items = [_compact_value(item) for item in value]
        compact_items = [item for item in compact_items if item is not None]
        return compact_items if compact_items else None
    if isinstance(value, Mapping):
        compact_map: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key.startswith("_"):
                continue
            compact_item = _compact_value(item)
            if compact_item is not None:
                compact_map[key] = compact_item
        return compact_map or None
    return str(value)


def _field_by_id(
    contract: ToolProjectionContract,
    field_id: str,
) -> ToolFieldContract | None:
    for field in contract.fields:
        if field.concept_id == field_id:
            return field
    return None


def _wire_key_value(concept_id: str) -> str | None:
    doc = load_concept(concept_id)
    attributes = doc.get("attributes") if isinstance(doc, Mapping) else None
    if isinstance(attributes, Mapping):
        return _clean_str(attributes.get("wire_key"))
    return None


def _payload_path_value(concept_id: str) -> str | None:
    doc = load_concept(concept_id)
    attributes = doc.get("attributes") if isinstance(doc, Mapping) else None
    if isinstance(attributes, Mapping):
        return _clean_str(attributes.get("payload_path"))
    return None


def _fallback_field_key(field_id: str) -> str:
    cleaned = field_id.removeprefix("#V#")
    for suffix in ("_field", "_argument_field"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    return cleaned


def _relationships(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    relationships = doc.get("relationships")
    return relationships if isinstance(relationships, Mapping) else {}


def _relationship_targets(doc: Mapping[str, Any] | None, predicate: str) -> tuple[str, ...]:
    if not isinstance(doc, Mapping):
        return ()
    return tuple(_normalise_unique(_relationships(doc).get(predicate)))


def _normalise_unique(value: Any) -> list[str]:
    return _ordered_unique(normalise_relationship_targets(value))


def _ordered_unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(_ordered_unique(list(values)))


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "ToolFieldContract",
    "ToolProjectionContract",
    "project_tool_payload_for_llm",
    "resolve_tool_projection_contract",
]