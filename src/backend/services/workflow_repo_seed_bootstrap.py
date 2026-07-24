"""Generic bootstrap helpers for repo-side workflow seed bundles."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import concept_service
from .text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
    upsert_text_for_concept,
)
from .workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from .workflow_vontology_materialisation_helpers import (
    ensure_instance_typing,
    suspend_event_workflow_integration,
)
from ..workflows import workflow_concept_authority_service as authority_service
from ..workflows import workflow_repo_seed_export_service as seed_export_service
from ..workflows.static_input_binding_utils import stable_static_input_bindings
from ..workflows.vontology_loader import (
    build_workflow_process_graph,
    load_workflow_definition_from_vontology,
    resolve_workflow_launch_contract,
    resolve_workflow_launch_input_contract,
    resolve_workflow_publication_lifecycle,
)
from ..workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)
from ..workflows.workflow_launch_input_contracts import (
    normalise_workflow_launch_input_contract,
)

_WORKFLOW_STEP_TYPE_ID = "#V#workflow_step"
_REQUIRED_AUTHORITY_SURFACE_LAUNCH_CONTRACT = "launch_contract"
_REQUIRED_AUTHORITY_SURFACE_LAUNCH_INPUT_CONTRACT = "launch_input_contract"
_REPO_SEED_VERSION_TEXT_PREDICATE = "#V#hasWorkflowRepoSeedVersionJson"
_REPO_SEED_VERSION_SCHEMA_VERSION = "workflow_repo_seed_version.v1"
_REPO_SEED_MIGRATION_RECEIPT_SCHEMA_VERSION = "workflow_repo_seed_migration_receipt.v1"
_SUPPORT_CONCEPT_MATERIALISATION_RECEIPT_SCHEMA_VERSION = (
    "workflow_support_concept_materialisation_receipt.v1"
)
_SUPPORT_CONCEPT_MATERIALISATION_RECEIPT_ATTRIBUTE = (
    "workflow_support_concept_materialisation_receipt"
)
_DEFAULT_REPO_SEED_VERSION_TEXT_MAX_TIME_MS = 15000
_REPO_SEED_VERSION_TEXT_MAX_TIME_MS_ENV = "VON_WORKFLOW_POLICY_TEXT_MAX_TIME_MS"


def _repo_seed_version_text_max_time_ms() -> int:
    raw = os.getenv(_REPO_SEED_VERSION_TEXT_MAX_TIME_MS_ENV)
    try:
        parsed = int(str(raw or "").strip())
    except (TypeError, ValueError):
        parsed = _DEFAULT_REPO_SEED_VERSION_TEXT_MAX_TIME_MS
    return max(1000, min(parsed, 120000))


def _normalise_support_concept_targets(raw_value: Any) -> list[str]:
    if isinstance(raw_value, str):
        raw_items: Sequence[Any] = (raw_value,)
    elif isinstance(raw_value, Sequence) and not isinstance(
        raw_value, (str, bytes, bytearray)
    ):
        raw_items = raw_value
    else:
        return []
    targets: list[str] = []
    for item in raw_items:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned and cleaned not in targets:
            targets.append(cleaned)
    return targets


def _scope_support_concepts_to_authority_payloads(
    raw_specs: Any,
    authority_payloads: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Return support concepts referenced by explicitly targeted workflows.

    A bundle can contain shared support concepts for many workflows.  A narrow
    ``target_workflow_ids`` repair must not acquire authority to rewrite every
    unrelated support concept in that bundle.  Keep directly referenced
    concepts plus their transitive support-concept dependencies; full-family
    bootstrap still receives the complete list.
    """

    if not isinstance(raw_specs, Sequence) or isinstance(
        raw_specs, (str, bytes, bytearray)
    ):
        return []
    specs = [spec for spec in raw_specs if isinstance(spec, Mapping)]
    by_id = {
        str(spec.get("concept_id") or "").strip(): spec
        for spec in specs
        if str(spec.get("concept_id") or "").strip()
    }
    if not by_id:
        return []

    def _referenced_support_ids(value: Any) -> set[str]:
        try:
            serialised = json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            serialised = str(value)
        return {
            concept_id
            for concept_id in by_id
            if re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(concept_id)}(?![A-Za-z0-9_])",
                serialised,
            )
        }

    selected_ids = {
        concept_id
        for concept_id, spec in by_id.items()
        if spec.get("materialise_for_targeted_workflow_bootstrap") is True
    }
    selected_ids.update(_referenced_support_ids(authority_payloads))
    pending = list(selected_ids)
    while pending:
        concept_id = pending.pop()
        spec = by_id.get(concept_id)
        if spec is None:
            continue
        for dependency_id in _referenced_support_ids(spec):
            if dependency_id in selected_ids:
                continue
            selected_ids.add(dependency_id)
            pending.append(dependency_id)
    return [spec for spec in specs if str(spec.get("concept_id")) in selected_ids]


def _merge_support_relationship_targets(
    existing_value: Any, targets: Sequence[str]
) -> list[Any]:
    merged: list[Any] = []
    if isinstance(existing_value, list):
        merged.extend(existing_value)
    elif isinstance(existing_value, str) and existing_value.strip():
        merged.append(existing_value.strip())
    elif existing_value is not None:
        merged.append(existing_value)

    seen_strings = {item for item in merged if isinstance(item, str)}
    for target in targets:
        if target in seen_strings:
            continue
        merged.append(target)
        seen_strings.add(target)
    return merged


def _support_concept_target_authority_payload(spec: Mapping[str, Any]) -> dict[str, Any]:
    relationships: dict[str, list[str]] = {}
    raw_relationships = spec.get("relationships")
    if isinstance(raw_relationships, Mapping):
        for raw_predicate, raw_targets in raw_relationships.items():
            predicate = str(raw_predicate or "").strip()
            targets = _normalise_support_concept_targets(raw_targets)
            if predicate and targets:
                relationships[predicate] = sorted(dict.fromkeys(targets))
    relation_values = _relation_spec_values(
        tuple(
            item
            for item in (spec.get("text_relations") or ())
            if isinstance(item, Mapping)
        )
    )
    return {
        "concept_id": str(spec.get("concept_id") or "").strip(),
        "type_surface": [
            {
                "parent_concept_id": parent_id,
                "relationship": (
                    "is_an_instance_of"
                    if bool(spec.get("create_as_instance"))
                    else "is_a_type_of"
                ),
            }
            for parent_id in sorted(
                {
                    str(item).strip()
                    for item in (spec.get("parent_concept_ids") or ())
                    if isinstance(item, str) and str(item).strip()
                }
            )
        ],
        "system_tags": sorted(
            {
                str(item).strip()
                for item in (spec.get("system_tags") or ())
                if isinstance(item, str) and str(item).strip()
            }
        ),
        "attributes": copy.deepcopy(dict(spec.get("attributes") or {})),
        "human_text_relations": _support_concept_human_text_relations(spec),
        "relationships": relationships,
        "text_relations": [
            {"predicate": predicate, "lang": lang, "text": text}
            for (predicate, lang), text in sorted(relation_values.items())
        ],
    }


def _support_concept_human_text_relations(
    spec: Mapping[str, Any],
) -> list[dict[str, str]]:
    relations: list[dict[str, str]] = []
    for field_name, predicate in (
        ("name", "hasName"),
        ("description", "hasDescription"),
        ("notes", "hasNote"),
    ):
        text = str(spec.get(field_name) or "").strip()
        if text:
            relations.append(
                {"predicate": predicate, "lang": "en-NZ", "text": text}
            )
    return relations


def _support_concept_pending_receipt(
    *,
    concept_id: str,
    target_authority_payload_sha256: str,
    source_tag: str | None,
    managed_by: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": _SUPPORT_CONCEPT_MATERIALISATION_RECEIPT_SCHEMA_VERSION,
        "status": "pending",
        "concept_id": concept_id,
        "target_authority_payload_sha256": target_authority_payload_sha256,
        "source_tag": source_tag,
        "managed_by": managed_by,
    }


def _support_concept_pending_receipt_is_valid(
    value: Any,
    *,
    concept_id: str,
    target_authority_payload_sha256: str,
) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("schema_version")
        == _SUPPORT_CONCEPT_MATERIALISATION_RECEIPT_SCHEMA_VERSION
        and value.get("status") == "pending"
        and str(value.get("concept_id") or "").strip() == concept_id
        and str(value.get("target_authority_payload_sha256") or "")
        .strip()
        .lower()
        == target_authority_payload_sha256
    )


def _support_concept_authority_status(
    *,
    concept_doc: Mapping[str, Any],
    spec: Mapping[str, Any],
    target_payload: Mapping[str, Any],
) -> tuple[bool, bool]:
    """Return (exact, resumable) for the mutable scoped support authority."""

    relationships = concept_doc.get("relationships")
    relationships = relationships if isinstance(relationships, Mapping) else {}
    static_surface_exact = True
    for type_expectation in target_payload.get("type_surface") or ():
        relationship = str(type_expectation.get("relationship") or "").strip()
        parent_concept_id = str(
            type_expectation.get("parent_concept_id") or ""
        ).strip()
        observed_targets = set(
            _normalise_support_concept_targets(relationships.get(relationship))
        )
        if parent_concept_id not in observed_targets:
            static_surface_exact = False
    observed_system_tags = {
        str(item).strip()
        for item in (concept_doc.get("system_tags") or ())
        if isinstance(item, str) and str(item).strip()
    }
    if not set(target_payload.get("system_tags") or ()).issubset(
        observed_system_tags
    ):
        static_surface_exact = False
    observed_attributes = concept_doc.get("attributes")
    observed_attributes = (
        observed_attributes if isinstance(observed_attributes, Mapping) else {}
    )
    for key, expected_value in dict(target_payload.get("attributes") or {}).items():
        if observed_attributes.get(key) != expected_value:
            static_surface_exact = False
    human_text_exact = True
    concept_id = str(
        concept_doc.get("concept_id") or spec.get("concept_id") or ""
    ).strip()
    for target_relation in target_payload.get("human_text_relations") or ():
        predicate = str(target_relation.get("predicate") or "").strip()
        lang = str(target_relation.get("lang") or "en-NZ").strip() or "en-NZ"
        expected = str(target_relation.get("text") or "").strip()
        observed_values = {
            str(row.get("text") or "").strip()
            for row in get_texts_for_concept(
                concept_id,
                predicate=predicate,
                limit=50,
            )
            if isinstance(row, Mapping)
            and (str(row.get("lang") or "en-NZ").strip() or "en-NZ") == lang
            and str(row.get("text") or "").strip()
        }
        if expected not in observed_values:
            human_text_exact = False
    relationship_exact = True
    for predicate, targets in dict(target_payload.get("relationships") or {}).items():
        observed_targets = set(
            _normalise_support_concept_targets(relationships.get(predicate))
        )
        if not set(targets).issubset(observed_targets):
            relationship_exact = False

    text_exact = True
    text_resumable = True
    for target_relation in target_payload.get("text_relations") or ():
        predicate = str(target_relation.get("predicate") or "").strip()
        lang = str(target_relation.get("lang") or "en-NZ").strip() or "en-NZ"
        expected = str(target_relation.get("text") or "").strip()
        observed_values = {
            str(row.get("text") or "").strip()
            for row in get_texts_for_concept(
                concept_id,
                predicate=predicate,
                limit=20,
            )
            if isinstance(row, Mapping)
            and (str(row.get("lang") or "en-NZ").strip() or "en-NZ") == lang
            and str(row.get("text") or "").strip()
        }
        if observed_values != {expected}:
            text_exact = False
        if observed_values - {expected}:
            text_resumable = False
    return (
        static_surface_exact
        and human_text_exact
        and relationship_exact
        and text_exact,
        static_surface_exact and text_resumable,
    )


def _materialise_support_concepts(
    raw_specs: Any,
    *,
    source_tag: str | None,
    managed_by: str | None,
    update_existing: bool = True,
) -> dict[str, Any]:
    """Materialise prerequisite non-workflow concepts declared by a seed bundle."""

    if not isinstance(raw_specs, Sequence) or isinstance(raw_specs, (str, bytes)):
        return {"created_concept_ids": [], "existing_concept_ids": [], "errors": []}

    created_concept_ids: list[str] = []
    existing_concept_ids: list[str] = []
    relationship_updated_concept_ids: list[str] = []
    text_relation_updated_concept_ids: list[str] = []
    errors: list[dict[str, Any]] = []
    for spec in raw_specs:
        if not isinstance(spec, Mapping):
            continue
        concept_id = str(spec.get("concept_id") or "").strip()
        name = str(spec.get("name") or "").strip()
        if not concept_id or not name:
            errors.append(
                {
                    "concept_id": concept_id or None,
                    "reason_code": "support_concept_identity_missing",
                }
            )
            continue
        target_authority_payload = _support_concept_target_authority_payload(spec)
        target_authority_sha256 = _stable_payload_sha256(target_authority_payload)
        pending_receipt = _support_concept_pending_receipt(
            concept_id=concept_id,
            target_authority_payload_sha256=target_authority_sha256,
            source_tag=source_tag,
            managed_by=managed_by,
        )
        try:
            existing = concept_service.get_concept_by_concept_id_exact(concept_id)
        except concept_service.ConceptNotFoundError:
            existing = None
        except Exception as exc:
            errors.append(
                {
                    "concept_id": concept_id,
                    "reason_code": "support_concept_authority_read_failed",
                    "error": str(exc),
                }
            )
            continue
        concept_doc: Mapping[str, Any] | None = (
            existing if isinstance(existing, Mapping) else None
        )
        if isinstance(concept_doc, Mapping):
            existing_concept_ids.append(concept_id)
            if not update_existing:
                try:
                    authority_exact, authority_resumable = (
                        _support_concept_authority_status(
                            concept_doc=concept_doc,
                            spec=spec,
                            target_payload=target_authority_payload,
                        )
                    )
                except Exception as exc:
                    errors.append(
                        {
                            "concept_id": concept_id,
                            "reason_code": "support_concept_authority_read_failed",
                            "error": str(exc),
                        }
                    )
                    continue
                attributes = concept_doc.get("attributes")
                attributes = attributes if isinstance(attributes, Mapping) else {}
                existing_receipt = attributes.get(
                    _SUPPORT_CONCEPT_MATERIALISATION_RECEIPT_ATTRIBUTE
                )
                pending_receipt_valid = (
                    _support_concept_pending_receipt_is_valid(
                        existing_receipt,
                        concept_id=concept_id,
                        target_authority_payload_sha256=target_authority_sha256,
                    )
                )
                if authority_exact:
                    # Exact represented authority is already usable regardless
                    # of whether it predates ownership receipts.
                    if pending_receipt_valid and isinstance(
                        existing_receipt, Mapping
                    ):
                        verified_attributes = dict(attributes)
                        verified_attributes[
                            _SUPPORT_CONCEPT_MATERIALISATION_RECEIPT_ATTRIBUTE
                        ] = {**dict(existing_receipt), "status": "verified"}
                        try:
                            concept_doc = concept_service.update_concept(
                                str(concept_doc.get("concept_id") or concept_id),
                                {"attributes": verified_attributes},
                            )
                        except Exception as exc:
                            errors.append(
                                {
                                    "concept_id": concept_id,
                                    "reason_code": (
                                        "support_concept_receipt_verify_failed"
                                    ),
                                    "error": str(exc),
                                }
                            )
                    continue
                if not pending_receipt_valid or not authority_resumable:
                    errors.append(
                        {
                            "concept_id": concept_id,
                            "reason_code": (
                                "support_concept_existing_authority_requires_"
                                "explicit_migration"
                            ),
                            "target_authority_payload_sha256": (
                                target_authority_sha256
                            ),
                            "pending_materialisation_receipt_valid": (
                                pending_receipt_valid
                            ),
                        }
                    )
                    continue
        else:
            parent_ids = [
                str(item).strip()
                for item in (spec.get("parent_concept_ids") or [])
                if isinstance(item, str) and str(item).strip()
            ]
            attributes = dict(spec.get("attributes") or {})
            if source_tag:
                attributes.setdefault("repo_seed_source_tag", source_tag)
            if managed_by:
                attributes.setdefault("repo_seed_managed_by", managed_by)
            if not update_existing:
                attributes[
                    _SUPPORT_CONCEPT_MATERIALISATION_RECEIPT_ATTRIBUTE
                ] = pending_receipt
            try:
                concept_doc = concept_service.create_concept(
                    name=name,
                    concept_id=concept_id,
                    parent_concept_ids=parent_ids,
                    create_as_instance=bool(spec.get("create_as_instance")),
                    description=spec.get("description"),
                    notes=spec.get("notes"),
                    system_tags=[
                        str(item).strip()
                        for item in (spec.get("system_tags") or [])
                        if isinstance(item, str) and str(item).strip()
                    ],
                    attributes=attributes or None,
                    defer_text_relations=True,
                )
                created_concept_ids.append(concept_id)
            except Exception as exc:
                errors.append(
                    {
                        "concept_id": concept_id,
                        "reason_code": "support_concept_create_failed",
                        "error": str(exc),
                    }
                )
                continue

        raw_relationships = spec.get("relationships")
        if isinstance(raw_relationships, Mapping):
            relationships = dict((concept_doc or {}).get("relationships") or {})
            relationship_changed = False
            for predicate_raw, targets_raw in raw_relationships.items():
                predicate = str(predicate_raw or "").strip()
                targets = _normalise_support_concept_targets(targets_raw)
                if not predicate or not targets:
                    continue
                merged_targets = _merge_support_relationship_targets(
                    relationships.get(predicate),
                    targets,
                )
                if relationships.get(predicate) != merged_targets:
                    relationships[predicate] = merged_targets
                    relationship_changed = True
            if relationship_changed:
                try:
                    concept_doc = concept_service.update_concept(
                        str((concept_doc or {}).get("concept_id") or concept_id),
                        {"relationships": relationships},
                        defer_side_effects=True,
                    )
                    relationship_updated_concept_ids.append(concept_id)
                except Exception as exc:
                    errors.append(
                        {
                            "concept_id": concept_id,
                            "reason_code": "support_concept_relationship_update_failed",
                            "error": str(exc),
                        }
                    )

        text_relation_specs = [
            item
            for item in (spec.get("text_relations") or [])
            if isinstance(item, Mapping)
        ]
        if text_relation_specs:
            try:
                persisted_concept_id = str(
                    (concept_doc or {}).get("concept_id") or concept_id
                )
                authority_service.upsert_seed_bundle_text_relations(
                    subject_concept_id=persisted_concept_id,
                    relation_specs=tuple(text_relation_specs),
                    workflow_id=persisted_concept_id,
                    source_tag=source_tag,
                    managed_by=managed_by,
                )
                text_relation_updated_concept_ids.append(concept_id)
            except Exception as exc:
                errors.append(
                    {
                        "concept_id": concept_id,
                        "reason_code": "support_concept_text_relation_update_failed",
                        "error": str(exc),
                    }
                )

        if not update_existing:
            try:
                persisted_concept_id = str(
                    (concept_doc or {}).get("concept_id") or concept_id
                )
                for human_relation in _support_concept_human_text_relations(spec):
                    upsert_text_for_concept(
                        subject_concept_id=persisted_concept_id,
                        predicate=human_relation["predicate"],
                        text=human_relation["text"],
                        lang=human_relation["lang"],
                        context=(
                            {"name_type": "NL", "source": source_tag}
                            if human_relation["predicate"] == "hasName"
                            else {"source": source_tag}
                        ),
                    )
            except Exception as exc:
                errors.append(
                    {
                        "concept_id": concept_id,
                        "reason_code": "support_concept_human_text_update_failed",
                        "error": str(exc),
                    }
                )

        if not update_existing and isinstance(concept_doc, Mapping):
            try:
                refreshed = concept_service.get_concept_by_concept_id_exact(concept_id)
                if not isinstance(refreshed, Mapping):
                    raise RuntimeError("support_concept_missing_after_materialisation")
                authority_exact, _authority_resumable = (
                    _support_concept_authority_status(
                        concept_doc=refreshed,
                        spec=spec,
                        target_payload=target_authority_payload,
                    )
                )
                if not authority_exact:
                    raise RuntimeError("support_concept_authority_readback_mismatch")
                refreshed_attributes = refreshed.get("attributes")
                verified_attributes = (
                    dict(refreshed_attributes)
                    if isinstance(refreshed_attributes, Mapping)
                    else {}
                )
                verified_attributes[
                    _SUPPORT_CONCEPT_MATERIALISATION_RECEIPT_ATTRIBUTE
                ] = {**pending_receipt, "status": "verified"}
                concept_service.update_concept(
                    str(refreshed.get("concept_id") or concept_id),
                    {"attributes": verified_attributes},
                )
            except Exception as exc:
                errors.append(
                    {
                        "concept_id": concept_id,
                        "reason_code": "support_concept_authority_readback_failed",
                        "error": str(exc),
                    }
                )

    return {
        "created_concept_ids": created_concept_ids,
        "existing_concept_ids": existing_concept_ids,
        "relationship_updated_concept_ids": relationship_updated_concept_ids,
        "text_relation_updated_concept_ids": text_relation_updated_concept_ids,
        "errors": errors,
    }


def _stable_state_metadata_subset(state: Any) -> dict[str, Any]:
    metadata = getattr(state, "metadata", None)
    if not isinstance(metadata, dict):
        return {}
    comparable: dict[str, Any] = {}
    for key in (
        "invokes_workflow",
        "reads_context_keys",
        "writes_context_keys",
        "tool_output_context_mappings",
        "subworkflow_contract",
        "retry_policy",
        "mutation_authority",
    ):
        value = metadata.get(key)
        if value:
            comparable[key] = value
    return comparable


def _normalise_context_input_mapping_spec_signatures(
    mapping_specs: tuple[Any, ...],
) -> tuple[tuple[str, str, bool], ...]:
    signatures: list[tuple[str, str, bool]] = []
    for spec in mapping_specs:
        tool_param = str(getattr(spec, "tool_param", None) or "").strip()
        context_key = str(getattr(spec, "context_key", None) or "").strip()
        if tool_param and context_key:
            signatures.append(
                (
                    tool_param,
                    context_key,
                    bool(getattr(spec, "required", True)),
                )
            )
    return tuple(sorted(dict.fromkeys(signatures)))


def _normalise_tool_output_mapping_spec_signatures(
    mapping_specs: tuple[Any, ...],
) -> tuple[tuple[str, str], ...]:
    signatures: list[tuple[str, str]] = []
    for spec in mapping_specs:
        tool_output_field = str(getattr(spec, "tool_output_field", None) or "").strip()
        context_key = str(getattr(spec, "context_key", None) or "").strip()
        if tool_output_field and context_key:
            signatures.append((tool_output_field, context_key))
    return tuple(sorted(dict.fromkeys(signatures)))


def _normalise_static_input_bindings(
    bindings: tuple[tuple[str, Any], ...],
) -> tuple[tuple[str, Any], ...]:
    return stable_static_input_bindings(bindings)


def _normalise_string_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            dict.fromkeys(
                str(item or "").strip() for item in values if str(item or "").strip()
            )
        )
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_seed_version(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _split_seed_version_token(value: str) -> tuple[Any, ...]:
    pieces: list[Any] = []
    current = ""
    current_is_digit: bool | None = None
    for char in value.strip():
        is_digit = char.isdigit()
        if not char.isalnum():
            if current:
                pieces.append(int(current) if current_is_digit else current.lower())
            current = ""
            current_is_digit = None
            continue
        if current and current_is_digit is not None and is_digit != current_is_digit:
            pieces.append(int(current) if current_is_digit else current.lower())
            current = ""
        current += char
        current_is_digit = is_digit
    if current:
        pieces.append(int(current) if current_is_digit else current.lower())
    return tuple(pieces) if pieces else (value.strip().lower(),)


def _compare_seed_versions(left: str | None, right: str | None) -> int | None:
    """Compare two seed version strings.

    Returns 1 when left is newer, 0 when equal, -1 when older, and None when
    either side is missing.  Numeric components sort numerically; text
    components provide a deterministic fallback for labelled experimental
    versions.
    """

    left_text = _normalise_seed_version(left)
    right_text = _normalise_seed_version(right)
    if not left_text or not right_text:
        return None
    left_parts = _split_seed_version_token(left_text)
    right_parts = _split_seed_version_token(right_text)
    max_len = max(len(left_parts), len(right_parts))
    for index in range(max_len):
        left_part = left_parts[index] if index < len(left_parts) else 0
        right_part = right_parts[index] if index < len(right_parts) else 0
        if left_part == right_part:
            continue
        if isinstance(left_part, int) and isinstance(right_part, int):
            return 1 if left_part > right_part else -1
        return 1 if str(left_part) > str(right_part) else -1
    return 0


def _normalise_workflow_seed_authority_payload(item: Any) -> Any:
    if isinstance(item, Mapping):
        output: dict[str, Any] = {}
        for key, child in item.items():
            key_text = str(key)
            if (
                key_text == "selection_policy"
                and str(child or "").strip().lower() == "adaptive"
            ):
                continue
            if key_text == "validation_policy" and child == {
                "prompt_validation_policy": "fail"
            }:
                continue
            output[key_text] = _normalise_workflow_seed_authority_payload(child)
        return output
    if isinstance(item, Sequence) and not isinstance(
        item,
        (str, bytes, bytearray),
    ):
        return [
            _normalise_workflow_seed_authority_payload(child) for child in item
        ]
    return item


def _stable_payload_sha256(value: Any) -> str:

    payload = json.dumps(
        _normalise_workflow_seed_authority_payload(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _first_payload_mismatch_path(expected: Any, observed: Any, path: str = "$") -> str:
    if isinstance(expected, Mapping) and isinstance(observed, Mapping):
        for key in sorted(set(expected) | set(observed), key=str):
            if key not in expected or key not in observed:
                return f"{path}.{key}"
            mismatch = _first_payload_mismatch_path(
                expected[key],
                observed[key],
                f"{path}.{key}",
            )
            if mismatch:
                return mismatch
        return ""
    if (
        isinstance(expected, Sequence)
        and not isinstance(expected, (str, bytes, bytearray))
        and isinstance(observed, Sequence)
        and not isinstance(observed, (str, bytes, bytearray))
    ):
        if len(expected) != len(observed):
            return f"{path}.length"
        for index, (expected_item, observed_item) in enumerate(
            zip(expected, observed)
        ):
            mismatch = _first_payload_mismatch_path(
                expected_item,
                observed_item,
                f"{path}[{index}]",
            )
            if mismatch:
                return mismatch
        return ""
    return "" if expected == observed else path


def _payload_component_sha256_by_path(value: Any) -> dict[str, str]:
    components: dict[str, str] = {}

    def _walk(item: Any, path: str) -> None:
        item = _normalise_workflow_seed_authority_payload(item)
        if isinstance(item, Mapping):
            if not item:
                components[path] = _stable_payload_sha256(item)
                return
            for key, child in item.items():
                _walk(child, f"{path}.{key}")
            return
        if isinstance(item, Sequence) and not isinstance(
            item,
            (str, bytes, bytearray),
        ):
            identity_keys = ("state_id", "type_id")
            identity_key = next(
                (
                    key
                    for key in identity_keys
                    if all(
                        isinstance(child, Mapping) and child.get(key) is not None
                        for child in item
                    )
                ),
                None,
            )
            if identity_key:
                for child in item:
                    _walk(child, f"{path}[{identity_key}={child[identity_key]}]")
                return
            if all(
                isinstance(child, Mapping)
                and child.get("predicate") is not None
                and child.get("lang") is not None
                for child in item
            ):
                for child in item:
                    _walk(
                        child,
                        f"{path}[predicate={child['predicate']}|lang={child['lang']}]",
                    )
                return
            if not item:
                components[path] = _stable_payload_sha256(item)
                return
            for index, child in enumerate(item):
                _walk(child, f"{path}[{index}]")
            return
        components[path] = _stable_payload_sha256(item)

    _walk(value, "$")
    return components


def _authority_payload_is_source_target_partial(
    *,
    source_payload: Mapping[str, Any] | None,
    target_payload: Mapping[str, Any],
    current_payload: Mapping[str, Any],
) -> bool:
    missing_sha256 = _stable_payload_sha256({"missing": True})
    source_components = (
        _payload_component_sha256_by_path(source_payload)
        if isinstance(source_payload, Mapping)
        else {}
    )
    target_components = _payload_component_sha256_by_path(target_payload)
    current_components = _payload_component_sha256_by_path(current_payload)
    for path in set(source_components) | set(target_components) | set(current_components):
        observed = current_components.get(path, missing_sha256)
        allowed = {
            source_components.get(path, missing_sha256),
            target_components.get(path, missing_sha256),
        }
        if observed not in allowed:
            return False
    return True


def _relation_spec_values(
    relation_specs: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], str]:
    values: dict[tuple[str, str], str] = {}
    for spec in relation_specs:
        if not isinstance(spec, Mapping):
            continue
        predicate = str(spec.get("predicate") or "").strip()
        text = str(spec.get("text") or "").strip()
        lang = str(spec.get("lang") or "en-NZ").strip() or "en-NZ"
        if predicate and text:
            # Publication is singleton per predicate/language, so the final
            # represented value is the last declared value for that key.
            values[(predicate, lang)] = text
    return values


def _project_relation_authority(
    *,
    source_specs: Sequence[Mapping[str, Any]],
    scope_specs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    source_values = _relation_spec_values(source_specs)
    return [
        {
            "predicate": predicate,
            "lang": lang,
            "text": source_values.get((predicate, lang)),
        }
        for predicate, lang in sorted(_relation_spec_values(scope_specs))
    ]


def _workflow_seed_authority_payload_from_bundle_surfaces(
    *,
    workflow_id: str,
    publication_spec: Any,
    source_workflow_type_ids: Sequence[str],
    scoped_workflow_type_ids: Sequence[str],
    source_workflow_text_relations: Sequence[Mapping[str, Any]],
    scoped_workflow_text_relations: Sequence[Mapping[str, Any]],
    source_launch_input_contract: Mapping[str, Any] | None,
    source_step_text_relations: Mapping[str, Sequence[Mapping[str, Any]]],
    scoped_step_text_relations: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    definition = authority_service._build_definition_from_publication_spec(
        workflow_id=workflow_id,
        spec=publication_spec,
    )
    canonical_launch_input_contract: dict[str, Any] | None = None
    if isinstance(source_launch_input_contract, Mapping):
        canonical_launch_input_contract, launch_input_contract_error = (
            normalise_workflow_launch_input_contract(source_launch_input_contract)
        )
        if canonical_launch_input_contract is None:
            raise ValueError(
                "repo_seed_workflow_launch_input_contract_invalid:"
                f"{launch_input_contract_error or 'invalid_contract'}"
            )
    source_types = {
        str(item).strip()
        for item in source_workflow_type_ids
        if isinstance(item, str) and str(item).strip()
    }
    return {
        "workflow_id": workflow_id,
        "publication_spec": (
            seed_export_service._build_publication_spec_payload_from_definition(
                workflow_id=workflow_id,
                definition=definition,
            )
        ),
        "workflow_type_surface": [
            {
                "type_id": type_id,
                "is_an_instance_of": type_id in source_types,
                "is_a_type_of": False,
            }
            for type_id in sorted(
                {
                    str(item).strip()
                    for item in scoped_workflow_type_ids
                    if isinstance(item, str) and str(item).strip()
                }
            )
        ],
        "workflow_text_relations": _project_relation_authority(
            source_specs=source_workflow_text_relations,
            scope_specs=scoped_workflow_text_relations,
        ),
        "launch_input_contract": (
            copy.deepcopy(canonical_launch_input_contract)
            if canonical_launch_input_contract is not None
            else None
        ),
        "step_text_relations": {
            step_concept_id: _project_relation_authority(
                source_specs=tuple(
                    source_step_text_relations.get(step_concept_id) or ()
                ),
                scope_specs=scope_specs,
            )
            for step_concept_id, scope_specs in sorted(
                scoped_step_text_relations.items()
            )
        },
    }


def _live_relation_authority(
    *,
    concept_id: str,
    scope_specs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for predicate, lang in sorted(_relation_spec_values(scope_specs)):
        text: str | None = None
        rows = get_texts_for_concept(concept_id, predicate=predicate, limit=20)
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_lang = str(row.get("lang") or "en-NZ").strip() or "en-NZ"
            row_text = str(row.get("text") or "").strip()
            if row_lang == lang and row_text:
                text = row_text
                break
        output.append({"predicate": predicate, "lang": lang, "text": text})
    return output


def _load_live_workflow_seed_authority_payload(
    *,
    workflow_id: str,
    scoped_workflow_type_ids: Sequence[str],
    scoped_workflow_text_relations: Sequence[Mapping[str, Any]],
    scoped_launch_input_contract: Mapping[str, Any] | None,
    scoped_step_text_relations: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any] | None:
    definition = load_workflow_definition_from_vontology(workflow_id)
    if definition is None:
        return None
    concept_doc = concept_service.get_concept_by_concept_id(workflow_id)
    if not isinstance(concept_doc, Mapping):
        return None
    relationships = concept_doc.get("relationships")
    relationships = relationships if isinstance(relationships, Mapping) else {}

    def _targets(value: Any) -> set[str]:
        if isinstance(value, str):
            return {value.strip()} if value.strip() else set()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return set()
        return {
            str(item).strip()
            for item in value
            if isinstance(item, str) and str(item).strip()
        }

    instance_types = _targets(relationships.get("is_an_instance_of"))
    parent_types = _targets(relationships.get("is_a_type_of"))
    launch_input_contract, _source = resolve_workflow_launch_input_contract(
        workflow_id
    )
    if not isinstance(scoped_launch_input_contract, Mapping):
        launch_input_contract = None
    return {
        "workflow_id": workflow_id,
        "publication_spec": (
            seed_export_service._build_publication_spec_payload_from_definition(
                workflow_id=workflow_id,
                definition=definition,
            )
        ),
        "workflow_type_surface": [
            {
                "type_id": type_id,
                "is_an_instance_of": type_id in instance_types,
                "is_a_type_of": type_id in parent_types,
            }
            for type_id in sorted(
                {
                    str(item).strip()
                    for item in scoped_workflow_type_ids
                    if isinstance(item, str) and str(item).strip()
                }
            )
        ],
        "workflow_text_relations": _live_relation_authority(
            concept_id=workflow_id,
            scope_specs=scoped_workflow_text_relations,
        ),
        "launch_input_contract": (
            copy.deepcopy(dict(launch_input_contract))
            if isinstance(launch_input_contract, Mapping)
            else None
        ),
        "step_text_relations": {
            step_concept_id: _live_relation_authority(
                concept_id=step_concept_id,
                scope_specs=scope_specs,
            )
            for step_concept_id, scope_specs in sorted(
                scoped_step_text_relations.items()
            )
        },
    }


def _load_live_workflow_seed_partial_authority_payload(
    *,
    workflow_id: str,
    scoped_workflow_type_ids: Sequence[str],
    scoped_workflow_text_relations: Sequence[Mapping[str, Any]],
    scoped_launch_input_contract: Mapping[str, Any] | None,
    scoped_step_text_relations: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any] | None:
    """Read every present scoped surface even when no full definition loads."""

    payload: dict[str, Any] = {}
    definition = load_workflow_definition_from_vontology(workflow_id)
    try:
        concept_doc = concept_service.get_concept_by_concept_id_exact(workflow_id)
    except concept_service.ConceptNotFoundError:
        concept_doc = None
    concept_doc = concept_doc if isinstance(concept_doc, Mapping) else None
    if definition is not None or concept_doc is not None:
        payload["workflow_id"] = workflow_id
    if definition is not None:
        payload["publication_spec"] = (
            seed_export_service._build_publication_spec_payload_from_definition(
                workflow_id=workflow_id,
                definition=definition,
            )
        )

    relationships = (
        concept_doc.get("relationships")
        if isinstance(concept_doc, Mapping)
        else {}
    )
    relationships = relationships if isinstance(relationships, Mapping) else {}

    def _targets(value: Any) -> set[str]:
        if isinstance(value, str):
            return {value.strip()} if value.strip() else set()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return set()
        return {
            str(item).strip()
            for item in value
            if isinstance(item, str) and str(item).strip()
        }

    instance_types = _targets(relationships.get("is_an_instance_of"))
    parent_types = _targets(relationships.get("is_a_type_of"))
    type_surface = [
        {
            "type_id": type_id,
            "is_an_instance_of": type_id in instance_types,
            "is_a_type_of": type_id in parent_types,
        }
        for type_id in sorted(
            {
                str(item).strip()
                for item in scoped_workflow_type_ids
                if isinstance(item, str)
                and str(item).strip()
                and (
                    str(item).strip() in instance_types
                    or str(item).strip() in parent_types
                )
            }
        )
    ]
    if type_surface:
        payload["workflow_type_surface"] = type_surface

    live_workflow_relations = _live_relation_authority(
        concept_id=workflow_id,
        scope_specs=scoped_workflow_text_relations,
    )
    present_workflow_relations = [
        item for item in live_workflow_relations if item.get("text") is not None
    ]
    if present_workflow_relations:
        payload["workflow_text_relations"] = present_workflow_relations

    if isinstance(scoped_launch_input_contract, Mapping):
        launch_input_contract, _source = resolve_workflow_launch_input_contract(
            workflow_id
        )
        if isinstance(launch_input_contract, Mapping):
            payload["launch_input_contract"] = copy.deepcopy(
                dict(launch_input_contract)
            )

    live_step_relations: dict[str, list[dict[str, Any]]] = {}
    for step_concept_id, scope_specs in sorted(scoped_step_text_relations.items()):
        present_relations = [
            item
            for item in _live_relation_authority(
                concept_id=step_concept_id,
                scope_specs=scope_specs,
            )
            if item.get("text") is not None
        ]
        if present_relations:
            live_step_relations[step_concept_id] = present_relations
    if live_step_relations:
        payload["step_text_relations"] = live_step_relations
    return payload or None


def _load_workflow_repo_seed_version_marker(
    workflow_id: str,
) -> dict[str, Any] | None:
    rows = get_texts_for_concept(
        workflow_id,
        predicate=_REPO_SEED_VERSION_TEXT_PREDICATE,
        limit=5,
        max_time_ms=_repo_seed_version_text_max_time_ms(),
    )
    for row in rows:
        raw_text = row.get("text") if isinstance(row, Mapping) else None
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        try:
            payload = json.loads(raw_text)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        seed_version = _normalise_seed_version(payload.get("seed_version"))
        if not seed_version:
            continue
        raw_migration_receipt = payload.get("migration_receipt")
        migration_receipt = (
            {str(key): value for key, value in raw_migration_receipt.items()}
            if isinstance(raw_migration_receipt, Mapping)
            else None
        )
        return {
            "seed_version": seed_version,
            "authority_payload_sha256": str(
                payload.get("authority_payload_sha256") or ""
            ).strip().lower()
            or None,
            "migration_receipt": migration_receipt,
            "schema_version": str(payload.get("schema_version") or "").strip() or None,
            "family_id": str(payload.get("family_id") or "").strip() or None,
            "source_tag": str(payload.get("source_tag") or "").strip() or None,
            "relation_id": row.get("relation_id"),
            "text_value_id": row.get("text_value_id"),
        }
    return None


def _repo_seed_version_status_by_workflow(
    *,
    seed_version: str | None,
    workflow_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    status_by_workflow: dict[str, dict[str, Any]] = {}
    clean_seed_version = _normalise_seed_version(seed_version)
    for workflow_id in workflow_ids:
        marker = (
            _load_workflow_repo_seed_version_marker(workflow_id)
            if clean_seed_version
            else None
        )
        existing_version = (
            _normalise_seed_version(marker.get("seed_version"))
            if isinstance(marker, Mapping)
            else None
        )
        comparison = _compare_seed_versions(clean_seed_version, existing_version)
        blocked = comparison is not None and comparison <= 0
        if comparison is None:
            reason = (
                "seed_version_missing"
                if not clean_seed_version
                else "vontology_seed_version_missing"
            )
        elif comparison > 0:
            reason = "repo_seed_version_newer"
        elif comparison == 0:
            reason = "repo_seed_version_equal"
        else:
            reason = "vontology_seed_version_newer"
        status_by_workflow[workflow_id] = {
            "workflow_id": workflow_id,
            "seed_version": clean_seed_version,
            "vontology_seed_version": existing_version,
            "comparison": comparison,
            "blocked": blocked,
            "reason": reason,
            "marker": marker,
        }
    return status_by_workflow


def _suppress_repo_seed_snapshot_drift_when_vontology_version_is_current(
    *,
    materialisation_preflight: dict[str, Any],
    version_blocked_workflow_ids: Sequence[str],
    target_workflow_ids: Sequence[str],
) -> dict[str, Any]:
    """Let Vontology's equal/newer seed marker outrank a stale repo snapshot.

    Version markers must not hide actual missing/corrupt materialisation.  They
    only suppress bundle-snapshot drift for workflows whose current Vontology
    materialisation is otherwise healthy.
    """

    blocked = {
        str(workflow_id or "").strip()
        for workflow_id in version_blocked_workflow_ids
        if str(workflow_id or "").strip()
    }
    if not blocked:
        return materialisation_preflight
    if materialisation_preflight.get("drift_workflow_ids"):
        return materialisation_preflight

    snapshot_drift_ids = [
        str(workflow_id or "").strip()
        for workflow_id in (
            materialisation_preflight.get("bundle_snapshot_drift_workflow_ids") or []
        )
        if str(workflow_id or "").strip()
    ]
    if not snapshot_drift_ids:
        return materialisation_preflight

    suppressed_ids = [
        workflow_id for workflow_id in snapshot_drift_ids if workflow_id in blocked
    ]
    if not suppressed_ids:
        return materialisation_preflight

    remaining_snapshot_drift_ids = [
        workflow_id for workflow_id in snapshot_drift_ids if workflow_id not in blocked
    ]
    updated = dict(materialisation_preflight)
    updated["bundle_snapshot_drift_workflow_ids"] = remaining_snapshot_drift_ids
    updated["bundle_snapshot_drift_detected"] = bool(remaining_snapshot_drift_ids)
    if not remaining_snapshot_drift_ids:
        updated["bundle_snapshot_issue_codes"] = []
    else:
        status_by_id = materialisation_preflight.get("bundle_snapshot_status_by_id")
        if isinstance(status_by_id, Mapping):
            updated["bundle_snapshot_issue_codes"] = sorted(
                {
                    str(
                        (status_by_id.get(workflow_id) or {}).get("issue_code") or ""
                    ).strip()
                    for workflow_id in remaining_snapshot_drift_ids
                    if str(
                        (status_by_id.get(workflow_id) or {}).get("issue_code") or ""
                    ).strip()
                }
            )
    updated["repo_seed_version_suppressed_bundle_snapshot_drift_workflow_ids"] = (
        suppressed_ids
    )
    already_current = bool(target_workflow_ids) and not remaining_snapshot_drift_ids
    updated["already_current"] = already_current
    updated["drift_detected"] = not already_current
    return updated


def _upsert_workflow_repo_seed_version_marker(
    *,
    workflow_id: str,
    seed_version: str,
    family_id: str | None,
    source_tag: str | None,
    managed_by: str | None,
    asset_path: str,
    authority_payload_sha256: str,
) -> dict[str, Any]:
    payload = {
        "schema_version": _REPO_SEED_VERSION_SCHEMA_VERSION,
        "seed_version": seed_version,
        "family_id": family_id,
        "source_tag": source_tag,
        "managed_by": managed_by,
        "asset_path": asset_path,
        "authority_payload_sha256": authority_payload_sha256,
        "recorded_at_utc": _utc_now_iso(),
    }
    return upsert_singleton_text_relation(
        subject_concept_id=workflow_id,
        predicate=_REPO_SEED_VERSION_TEXT_PREDICATE,
        text=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={
            key: value
            for key, value in {
                "workflow_id": workflow_id,
                "family_id": family_id,
                "source": source_tag,
                "managed_by": managed_by,
                "repo_seed_role": "version_marker_only",
            }.items()
            if value is not None
        },
        garbage_collect=True,
    )


def _upsert_workflow_repo_seed_pending_migration_receipt(
    *,
    workflow_id: str,
    source_seed_version: str | None,
    source_authority_payload: Mapping[str, Any],
    source_authority_payload_sha256: str,
    source_authority_absent: bool = False,
    target_seed_version: str,
    target_authority_payload_sha256: str,
    family_id: str | None,
    source_tag: str | None,
    managed_by: str | None,
    asset_path: str,
) -> dict[str, Any]:
    receipt = {
        "schema_version": _REPO_SEED_MIGRATION_RECEIPT_SCHEMA_VERSION,
        "status": "pending",
        "source_seed_version": source_seed_version or "unversioned",
        "source_authority_payload": copy.deepcopy(dict(source_authority_payload)),
        "source_authority_payload_sha256": source_authority_payload_sha256,
        "source_authority_absent": bool(source_authority_absent),
        "target_seed_version": target_seed_version,
        "target_authority_payload_sha256": target_authority_payload_sha256,
    }
    payload = {
        "schema_version": _REPO_SEED_VERSION_SCHEMA_VERSION,
        "seed_version": source_seed_version or "unversioned",
        "family_id": family_id,
        "source_tag": source_tag,
        "managed_by": managed_by,
        "asset_path": asset_path,
        "migration_receipt": receipt,
        "recorded_at_utc": _utc_now_iso(),
    }
    return upsert_singleton_text_relation(
        subject_concept_id=workflow_id,
        predicate=_REPO_SEED_VERSION_TEXT_PREDICATE,
        text=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={
            "workflow_id": workflow_id,
            "repo_seed_role": "pending_migration_receipt",
        },
        garbage_collect=True,
    )


def _prune_shadowed_static_input_bindings(
    *,
    static_input_bindings: tuple[tuple[str, Any], ...],
    context_input_mapping_specs: tuple[tuple[str, str, bool], ...],
) -> tuple[tuple[str, Any], ...]:
    mapped_tool_params = {
        tool_param
        for tool_param, _context_key, _required in context_input_mapping_specs
    }
    if not mapped_tool_params:
        return static_input_bindings
    return tuple(
        binding
        for binding in static_input_bindings
        if binding[0] not in mapped_tool_params
    )


def _extract_context_binding_key(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in (
        "$context_key",
        "context_key",
        "workflow_context_key",
        "from_context_key",
    ):
        candidate = str(value.get(key) or "").strip()
        if candidate:
            return candidate
    return None


def _step_concept_id_by_state(
    *,
    workflow_id: str,
    spec: Any,
) -> dict[str, str]:
    ordered_ids = authority_service.publication_spec_step_concept_ids(
        workflow_id=workflow_id,
        spec=spec,
    )
    return {
        step.state_id: step_concept_id
        for step, step_concept_id in zip(spec.steps, ordered_ids)
        if str(getattr(step, "state_id", "") or "").strip() and step_concept_id
    }


def _bundle_declares_explicit_launch_contract(
    relation_specs: Sequence[Mapping[str, Any]] | None,
) -> bool:
    for relation_spec in relation_specs or ():
        if not isinstance(relation_spec, Mapping):
            continue
        predicate = str(relation_spec.get("predicate") or "").strip()
        if predicate in ("#V#has_launch_contract", "has_launch_contract"):
            return True
    return False


def _resolve_missing_required_authority_surfaces(
    *,
    workflow_id: str,
    workflow_text_relations: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    workflow_launch_input_contracts: Mapping[str, Mapping[str, Any]] | None,
) -> list[str]:
    missing_surfaces: list[str] = []

    if workflow_id in (workflow_launch_input_contracts or {}):
        launch_input_contract, _launch_input_source = (
            resolve_workflow_launch_input_contract(workflow_id)
        )
        if not isinstance(launch_input_contract, dict):
            missing_surfaces.append(_REQUIRED_AUTHORITY_SURFACE_LAUNCH_INPUT_CONTRACT)

    relation_specs = (
        workflow_text_relations.get(workflow_id)
        if isinstance(workflow_text_relations, Mapping)
        else ()
    )
    if _bundle_declares_explicit_launch_contract(relation_specs):
        launch_contract, _launch_contract_source = resolve_workflow_launch_contract(
            workflow_id
        )
        if not isinstance(launch_contract, dict):
            missing_surfaces.append(_REQUIRED_AUTHORITY_SURFACE_LAUNCH_CONTRACT)

    return missing_surfaces


def _resolve_loaded_state_entry(
    *,
    workflow_id: str,
    loaded_definition: Any,
    step: Any,
    step_concept_id_by_state: dict[str, str],
) -> tuple[str, Any] | None:
    state_id = str(getattr(step, "state_id", "") or "").strip()
    if not state_id:
        return None
    states_raw = getattr(loaded_definition, "states", None)
    states = states_raw if isinstance(states_raw, dict) else {}
    loaded_state = states.get(state_id)
    if loaded_state is not None:
        return state_id, loaded_state
    explicit_concept_id = str(getattr(step, "concept_id", "") or "").strip()
    if explicit_concept_id:
        loaded_state = states.get(explicit_concept_id)
        if loaded_state is not None:
            return explicit_concept_id, loaded_state
    computed_concept_id = step_concept_id_by_state.get(state_id)
    if computed_concept_id:
        loaded_state = states.get(computed_concept_id)
        if loaded_state is not None:
            return computed_concept_id, loaded_state
        default_concept_id = authority_service._step_concept_id(
            workflow_id=workflow_id,
            state_id=state_id,
        )
        loaded_state = states.get(default_concept_id)
        if loaded_state is not None:
            return default_concept_id, loaded_state
    return None


def _stable_loaded_action_surface(
    *,
    workflow_id: str,
    state_id: str,
    loaded_state: Any,
    loaded_definition: Any,
) -> dict[str, Any]:
    actions = tuple(getattr(loaded_state, "actions", ()) or ())
    if len(actions) > 1:
        return {"invalid_action_count": len(actions)}
    action = actions[0] if actions else None
    runtime_details = authority_service._extract_runtime_step_publication_details(
        workflow_id=workflow_id,
        state_id=state_id,
        registration_definition=loaded_definition,
    )
    context_input_mapping_specs = _normalise_context_input_mapping_spec_signatures(
        runtime_details.context_input_mapping_specs
    )
    static_input_bindings = _prune_shadowed_static_input_bindings(
        static_input_bindings=_normalise_static_input_bindings(
            runtime_details.static_input_bindings
        ),
        context_input_mapping_specs=context_input_mapping_specs,
    )
    static_input_bindings = tuple(
        binding
        for binding in static_input_bindings
        if binding[0] != "__prompt_resolution_diagnostics"
    )
    return {
        "action_id": (
            str(getattr(action, "action_id", "") or "").strip() or None
            if action is not None
            else None
        ),
        "action_concept_id": (
            str(getattr(action, "contract_concept_id", "") or "").strip() or None
            if action is not None
            else None
        ),
        "execution_mode": (
            str(getattr(action, "execution_mode", "") or "").strip() or None
            if action is not None
            else None
        ),
        "prompt_concept_ids": (
            authority_service._extract_publication_prompt_concept_ids(action)
            if action is not None
            else ()
        ),
        "llm_policy": (
            dict(runtime_details.llm_policy)
            if isinstance(runtime_details.llm_policy, dict)
            else None
        ),
        "validation_policy": (
            dict(runtime_details.validation_policy)
            if isinstance(runtime_details.validation_policy, dict)
            else None
        ),
        "mutation_authority": (
            dict(runtime_details.mutation_authority)
            if isinstance(runtime_details.mutation_authority, dict)
            else None
        ),
        "invoked_workflow_id": (
            str(runtime_details.invoked_workflow_id or "").strip() or None
        ),
        "static_input_bindings": static_input_bindings,
        "context_input_mapping_specs": context_input_mapping_specs,
        "tool_output_mapping_specs": _normalise_tool_output_mapping_spec_signatures(
            runtime_details.tool_output_mapping_specs
        ),
        "writes_context_keys": _normalise_string_tuple(
            runtime_details.writes_context_keys
        ),
    }


def _stable_expected_action_surface(step: Any) -> dict[str, Any]:
    action_id = str(getattr(step, "action_id", "") or "").strip() or None
    execution_mode = str(getattr(step, "execution_mode", "") or "").strip() or None
    if action_id and not execution_mode:
        execution_mode = "deterministic"
    context_input_mapping_specs = list(
        _normalise_context_input_mapping_spec_signatures(
            getattr(step, "context_input_mapping_specs", ()) or ()
        )
    )
    static_input_bindings_raw = _normalise_static_input_bindings(
        getattr(step, "static_input_bindings", ()) or ()
    )
    lifted_context_bindings: list[tuple[str, str, bool]] = []
    remaining_static_input_bindings: list[tuple[str, Any]] = []
    for tool_param, value in static_input_bindings_raw:
        context_key = _extract_context_binding_key(value)
        if context_key:
            lifted_context_bindings.append((tool_param, context_key, True))
            continue
        remaining_static_input_bindings.append((tool_param, value))
    context_input_mapping_specs = tuple(
        sorted(dict.fromkeys([*context_input_mapping_specs, *lifted_context_bindings]))
    )
    static_input_bindings = _prune_shadowed_static_input_bindings(
        static_input_bindings=tuple(remaining_static_input_bindings),
        context_input_mapping_specs=context_input_mapping_specs,
    )
    return {
        "action_id": action_id,
        "action_concept_id": (
            str(getattr(step, "action_concept_id", "") or "").strip() or None
        ),
        "execution_mode": execution_mode,
        "prompt_concept_ids": tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in getattr(step, "prompt_concept_ids", ()) or ()
                if str(item or "").strip()
            )
        ),
        "llm_policy": (
            dict(step.llm_policy)
            if isinstance(getattr(step, "llm_policy", None), dict)
            else None
        ),
        "validation_policy": (
            dict(step.validation_policy)
            if isinstance(getattr(step, "validation_policy", None), dict)
            else None
        ),
        "mutation_authority": (
            dict(step.mutation_authority)
            if isinstance(getattr(step, "mutation_authority", None), dict)
            else None
        ),
        "invoked_workflow_id": (
            str(getattr(step, "invoked_workflow_id", "") or "").strip() or None
        ),
        "static_input_bindings": static_input_bindings,
        "context_input_mapping_specs": context_input_mapping_specs,
        "tool_output_mapping_specs": _normalise_tool_output_mapping_spec_signatures(
            getattr(step, "tool_output_mapping_specs", ()) or ()
        ),
        "writes_context_keys": _normalise_string_tuple(
            getattr(step, "writes_context_keys", ()) or ()
        ),
    }


def _stable_expected_state_metadata_subset(
    *,
    workflow_id: str,
    state_id: str,
    step: Any,
) -> dict[str, Any]:
    synthetic_definition = authority_service._build_definition_from_publication_spec(
        workflow_id=workflow_id,
        spec=authority_service._CanonicalWorkflowPublicationSpec(
            initial_state=state_id,
            steps=(step,),
        ),
    )
    return _stable_state_metadata_subset(synthetic_definition.states[state_id])


def _action_surfaces_match(
    *,
    loaded_surface: dict[str, Any],
    expected_surface: dict[str, Any],
    step: Any,
) -> bool:
    exact_match_keys = (
        "action_id",
        "action_concept_id",
        "execution_mode",
        "prompt_concept_ids",
        "invoked_workflow_id",
        "static_input_bindings",
        "tool_output_mapping_specs",
        "writes_context_keys",
    )
    for key in exact_match_keys:
        if loaded_surface.get(key) != expected_surface.get(key):
            return False

    if not _context_input_mapping_specs_match(
        loaded_specs=loaded_surface.get("context_input_mapping_specs"),
        expected_specs=expected_surface.get("context_input_mapping_specs"),
        step=step,
    ):
        return False

    subset_match_keys = (
        "llm_policy",
        "validation_policy",
        "mutation_authority",
    )
    for key in subset_match_keys:
        expected_value = expected_surface.get(key)
        if expected_value is None:
            continue
        if key not in loaded_surface or not _materialisation_value_matches(
            loaded_value=loaded_surface.get(key),
            expected_value=expected_value,
        ):
            return False
    return True


def _state_reference_matches(
    *,
    workflow_id: str,
    expected_state_id: str,
    actual_state_ref: Any,
    step_concept_id_by_state: dict[str, str],
) -> bool:
    actual = str(actual_state_ref or "").strip()
    if not actual:
        return False
    expected_refs = {
        expected_state_id,
        authority_service._step_concept_id(
            workflow_id=workflow_id,
            state_id=expected_state_id,
        ),
    }
    expected_concept_id = step_concept_id_by_state.get(expected_state_id)
    if expected_concept_id:
        expected_refs.add(expected_concept_id)
    return actual in expected_refs


def _normalise_condition_spec(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {"kind": "always"}


def _stable_loaded_transition_surface(loaded_state: Any) -> list[dict[str, Any]]:
    transitions = []
    for transition in tuple(getattr(loaded_state, "transitions", ()) or ()):
        transitions.append(
            {
                "to_state": str(getattr(transition, "to_state", "") or "").strip(),
                "reason": str(getattr(transition, "reason", "") or "").strip() or None,
                "condition_spec": _normalise_condition_spec(
                    getattr(transition, "condition_spec", None)
                ),
            }
        )
    return transitions


def _stable_expected_transition_surface(step: Any) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []
    for branch in getattr(step, "conditional_transitions", ()) or ():
        transitions.append(
            {
                "to_state": str(getattr(branch, "to_state", "") or "").strip(),
                "reason": str(getattr(branch, "reason", "") or "").strip() or None,
                "condition_spec": _normalise_condition_spec(
                    getattr(branch, "condition_spec", None)
                ),
            }
        )
    for attribute_name, reason, condition_spec in (
        (
            "on_failure_state",
            "on_failure",
            {"kind": "context_flag", "key": "last_action_failed", "expected": True},
        ),
        (
            "on_unknown_state",
            "on_unknown",
            {"kind": "context_flag", "key": "last_action_unknown", "expected": True},
        ),
        (
            "on_approval_required_state",
            "on_approval_required",
            {"kind": "context_flag", "key": "approval_required", "expected": True},
        ),
        ("on_break_state", "on_break", {"kind": "control_signal", "signal": "break"}),
        (
            "on_continue_state",
            "on_continue",
            {"kind": "control_signal", "signal": "continue"},
        ),
        (
            "on_true_state",
            "on_true",
            {"kind": "transition_result_truth", "expected": True},
        ),
        (
            "on_false_state",
            "on_false",
            {"kind": "transition_result_truth", "expected": False},
        ),
        ("next_state", "next_step", {"kind": "always"}),
    ):
        target_state = str(getattr(step, attribute_name, "") or "").strip()
        if target_state:
            transitions.append(
                {
                    "to_state": target_state,
                    "reason": reason,
                    "condition_spec": condition_spec,
                }
            )
    return transitions


def _transition_surfaces_match(
    *,
    workflow_id: str,
    step: Any,
    loaded_state: Any,
    step_concept_id_by_state: dict[str, str],
) -> bool:
    expected_transitions = _stable_expected_transition_surface(step)
    loaded_transitions = _stable_loaded_transition_surface(loaded_state)
    if len(expected_transitions) != len(loaded_transitions):
        return False

    unmatched = list(loaded_transitions)
    for expected in expected_transitions:
        match_index = next(
            (
                index
                for index, candidate in enumerate(unmatched)
                if candidate.get("reason") == expected.get("reason")
                and _materialisation_value_matches(
                    loaded_value=candidate.get("condition_spec"),
                    expected_value=expected.get("condition_spec"),
                )
                and _state_reference_matches(
                    workflow_id=workflow_id,
                    expected_state_id=str(expected.get("to_state") or "").strip(),
                    actual_state_ref=candidate.get("to_state"),
                    step_concept_id_by_state=step_concept_id_by_state,
                )
            ),
            None,
        )
        if match_index is None:
            return False
        unmatched.pop(match_index)
    return not unmatched


def _materialisation_value_matches(*, loaded_value: Any, expected_value: Any) -> bool:
    if loaded_value == expected_value:
        return True
    if isinstance(loaded_value, dict) and isinstance(expected_value, dict):
        for key, value in expected_value.items():
            if key not in loaded_value:
                return False
            if not _materialisation_value_matches(
                loaded_value=loaded_value.get(key),
                expected_value=value,
            ):
                return False
        return True
    if isinstance(loaded_value, list) and isinstance(expected_value, list):
        if len(loaded_value) != len(expected_value):
            return False
        return all(
            _materialisation_value_matches(
                loaded_value=item,
                expected_value=expected,
            )
            for item, expected in zip(loaded_value, expected_value)
        )
    return False


def _context_input_mapping_specs_match(
    *,
    loaded_specs: Any,
    expected_specs: Any,
    step: Any,
) -> bool:
    loaded_mapping = {
        (str(tool_param), str(context_key)): bool(required)
        for tool_param, context_key, required in (loaded_specs or ())
    }
    expected_mapping: dict[tuple[str, str], tuple[bool, str | None]] = {}
    for tool_param, context_key, required in expected_specs or ():
        key = (str(tool_param), str(context_key))
        if not key[0] or not key[1]:
            continue
        expected_mapping[key] = (bool(required), None)
    for spec in getattr(step, "context_input_mapping_specs", ()) or ():
        key = (
            str(getattr(spec, "tool_param", "") or "").strip(),
            str(getattr(spec, "context_key", "") or "").strip(),
        )
        if not key[0] or not key[1]:
            continue
        expected_mapping[key] = (
            bool(getattr(spec, "required", True)),
            str(getattr(spec, "concept_id", "") or "").strip() or None,
        )
    if set(loaded_mapping.keys()) != set(expected_mapping.keys()):
        return False

    for key, (expected_required, concept_id) in expected_mapping.items():
        loaded_required = bool(loaded_mapping.get(key))
        mapping_doc = None
        if concept_id:
            try:
                mapping_doc = concept_service.get_concept_by_concept_id(concept_id)
            except Exception:
                return False
        mapping_spec = ((mapping_doc or {}).get("concept_data") or {}).get(
            "workflow_mapping_spec"
        ) or {}
        stored_required = (
            mapping_spec.get("required") if isinstance(mapping_spec, dict) else None
        )

        if expected_required:
            if not loaded_required:
                return False
            continue

        if loaded_required:
            if stored_required is not True:
                return False
            continue

        if stored_required is True:
            return False
    return True


def _state_metadata_subset_matches(
    *,
    loaded_metadata: dict[str, Any],
    expected_metadata: dict[str, Any],
) -> bool:
    for key, value in expected_metadata.items():
        if key not in loaded_metadata:
            return False
        if key == "reads_context_keys":
            expected_keys = {
                str(item).strip() for item in (value or []) if str(item).strip()
            }
            loaded_keys = {
                str(item).strip()
                for item in (loaded_metadata.get(key) or [])
                if str(item).strip()
            }
            if not loaded_keys.issuperset(expected_keys):
                return False
            continue
        if not _materialisation_value_matches(
            loaded_value=loaded_metadata.get(key),
            expected_value=value,
        ):
            return False
    return True


def _materialisation_matches_publication_spec(
    *,
    loaded_definition: Any,
    workflow_id: str,
    publication_spec: Any,
) -> bool:
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        workflow_id = str(getattr(loaded_definition, "workflow_id", "") or "").strip()
    if not workflow_id:
        return False
    steps = tuple(getattr(publication_spec, "steps", ()) or ())
    step_concept_id_by_state = _step_concept_id_by_state(
        workflow_id=workflow_id,
        spec=publication_spec,
    )
    initial_state = str(getattr(publication_spec, "initial_state", "") or "").strip()
    if initial_state and not _state_reference_matches(
        workflow_id=workflow_id,
        expected_state_id=initial_state,
        actual_state_ref=getattr(loaded_definition, "initial_state", None),
        step_concept_id_by_state=step_concept_id_by_state,
    ):
        return False

    for step in steps:
        state_id = str(getattr(step, "state_id", "") or "").strip()
        if not state_id:
            return False
        loaded_state_entry = _resolve_loaded_state_entry(
            workflow_id=workflow_id,
            loaded_definition=loaded_definition,
            step=step,
            step_concept_id_by_state=step_concept_id_by_state,
        )
        if loaded_state_entry is None:
            return False
        loaded_state_id, loaded_state = loaded_state_entry
        expected_publication = _stable_expected_action_surface(step)
        loaded_surface = _stable_loaded_action_surface(
            workflow_id=workflow_id,
            state_id=loaded_state_id,
            loaded_state=loaded_state,
            loaded_definition=loaded_definition,
        )
        if not _action_surfaces_match(
            loaded_surface=loaded_surface,
            expected_surface=expected_publication,
            step=step,
        ):
            return False
        expected = _stable_expected_state_metadata_subset(
            workflow_id=workflow_id,
            state_id=state_id,
            step=step,
        )
        if not expected:
            expected = {}
        loaded_metadata = _stable_state_metadata_subset(loaded_state)
        if expected and not _state_metadata_subset_matches(
            loaded_metadata=loaded_metadata,
            expected_metadata=expected,
        ):
            return False
        if not _transition_surfaces_match(
            workflow_id=workflow_id,
            step=step,
            loaded_state=loaded_state,
            step_concept_id_by_state=step_concept_id_by_state,
        ):
            return False
    return True


def _validate_existing_materialisation(
    *,
    target_workflow_ids: tuple[str, ...],
    supported_action_ids: tuple[str, ...],
    publication_specs: dict[str, Any],
    workflow_text_relations: dict[str, tuple[dict[str, Any], ...]] | None = None,
    workflow_launch_input_contracts: dict[str, Mapping[str, Any]] | None = None,
) -> tuple[bool, dict[str, dict[str, Any]], dict[str, Any]]:
    if not target_workflow_ids:
        return (
            False,
            {},
            {
                "already_current": False,
                "drift_detected": True,
                "current_workflow_ids": [],
                "drift_workflow_ids": [],
                "issue_codes": ["no_target_workflow_ids"],
                "workflow_status_by_id": {},
                "bundle_snapshot_drift_detected": False,
                "bundle_snapshot_drift_workflow_ids": [],
                "bundle_snapshot_issue_codes": [],
                "bundle_snapshot_status_by_id": {},
            },
        )

    cached_definitions: dict[str, Any | None] = {}
    validation_by_workflow_id: dict[str, dict[str, Any]] = {}
    workflow_status_by_id: dict[str, dict[str, Any]] = {}
    bundle_snapshot_status_by_id: dict[str, dict[str, Any]] = {}

    def _cached_loader(candidate_workflow_id: str) -> Any | None:
        workflow_id = str(candidate_workflow_id or "").strip()
        if not workflow_id:
            return None
        if workflow_id not in cached_definitions:
            cached_definitions[workflow_id] = load_workflow_definition_from_vontology(
                workflow_id
            )
        return cached_definitions[workflow_id]

    for workflow_id in target_workflow_ids:
        workflow_status: dict[str, Any] = {
            "workflow_id": workflow_id,
            "status": "current",
            "issue_code": None,
        }
        graph, graph_warnings = build_workflow_process_graph(workflow_id)
        if not isinstance(graph, dict):
            workflow_status["status"] = "graph_missing"
            workflow_status["issue_code"] = "graph_missing"
            workflow_status_by_id[workflow_id] = workflow_status
            continue
        warning_items = [
            str(item).strip()
            for item in (graph_warnings or [])
            if isinstance(item, str) and str(item).strip()
        ]
        if warning_items:
            workflow_status["status"] = "graph_warnings"
            workflow_status["issue_code"] = "graph_warnings"
            workflow_status["warnings"] = list(warning_items)
            workflow_status_by_id[workflow_id] = workflow_status
            continue

        definition = _cached_loader(workflow_id)
        if definition is None:
            workflow_status["status"] = "definition_missing"
            workflow_status["issue_code"] = "definition_missing"
            workflow_status_by_id[workflow_id] = workflow_status
            continue

        publication_lifecycle, publication_lifecycle_source = (
            resolve_workflow_publication_lifecycle(workflow_id)
        )
        if (
            isinstance(publication_lifecycle, dict)
            and publication_lifecycle.get("published") is False
        ):
            workflow_status["status"] = "workflow_not_published"
            workflow_status["issue_code"] = "workflow_not_published"
            workflow_status["publication_lifecycle"] = dict(publication_lifecycle)
            workflow_status["publication_lifecycle_source"] = (
                str(publication_lifecycle_source or "").strip() or None
            )
            workflow_status_by_id[workflow_id] = workflow_status
            continue

        publication_spec = publication_specs.get(workflow_id)
        validation = validate_workflow_definition_contract(
            definition=definition,
            supported_action_ids=supported_action_ids,
            known_workflow_ids=target_workflow_ids,
            workflow_definition_loader=_cached_loader,
        )
        validation_by_workflow_id[workflow_id] = copy.deepcopy(validation)
        if not bool(validation.get("valid")):
            workflow_status["status"] = "definition_invalid"
            workflow_status["issue_code"] = "definition_invalid"
            workflow_status["errors"] = [
                str(item).strip()
                for item in (validation.get("errors") or [])
                if isinstance(item, str) and str(item).strip()
            ]
            workflow_status_by_id[workflow_id] = workflow_status
            continue

        missing_authority_surfaces = _resolve_missing_required_authority_surfaces(
            workflow_id=workflow_id,
            workflow_text_relations=workflow_text_relations,
            workflow_launch_input_contracts=workflow_launch_input_contracts,
        )
        if missing_authority_surfaces:
            workflow_status["status"] = "required_authority_surface_missing"
            workflow_status["issue_code"] = "required_authority_surface_missing"
            workflow_status["missing_authority_surfaces"] = list(
                missing_authority_surfaces
            )
            workflow_status_by_id[workflow_id] = workflow_status
            continue

        workflow_status_by_id[workflow_id] = workflow_status

        snapshot_status: dict[str, Any] = {
            "workflow_id": workflow_id,
            "status": "current",
            "issue_code": None,
        }
        if publication_spec is None:
            snapshot_status["status"] = "publication_spec_missing"
            snapshot_status["issue_code"] = "publication_spec_missing"
        elif not _materialisation_matches_publication_spec(
            loaded_definition=definition,
            workflow_id=workflow_id,
            publication_spec=publication_spec,
        ):
            snapshot_status["status"] = "definition_mismatch"
            snapshot_status["issue_code"] = "definition_mismatch"
        bundle_snapshot_status_by_id[workflow_id] = snapshot_status

    current_workflow_ids = [
        workflow_id
        for workflow_id, status in workflow_status_by_id.items()
        if status.get("status") == "current"
    ]
    drift_workflow_ids = [
        workflow_id
        for workflow_id, status in workflow_status_by_id.items()
        if status.get("status") != "current"
    ]
    issue_codes = sorted(
        {
            str(status.get("issue_code") or "").strip()
            for status in workflow_status_by_id.values()
            if str(status.get("issue_code") or "").strip()
        }
    )
    bundle_snapshot_drift_workflow_ids = [
        workflow_id
        for workflow_id, status in bundle_snapshot_status_by_id.items()
        if status.get("status") != "current"
    ]
    bundle_snapshot_issue_codes = sorted(
        {
            str(status.get("issue_code") or "").strip()
            for status in bundle_snapshot_status_by_id.values()
            if str(status.get("issue_code") or "").strip()
        }
    )
    already_current = (
        bool(target_workflow_ids)
        and not drift_workflow_ids
        and not bundle_snapshot_drift_workflow_ids
    )

    return (
        already_current,
        validation_by_workflow_id,
        {
            "already_current": already_current,
            "drift_detected": not already_current,
            "current_workflow_ids": current_workflow_ids,
            "drift_workflow_ids": drift_workflow_ids,
            "issue_codes": issue_codes,
            "workflow_status_by_id": workflow_status_by_id,
            "bundle_snapshot_drift_detected": bool(bundle_snapshot_drift_workflow_ids),
            "bundle_snapshot_drift_workflow_ids": bundle_snapshot_drift_workflow_ids,
            "bundle_snapshot_issue_codes": bundle_snapshot_issue_codes,
            "bundle_snapshot_status_by_id": bundle_snapshot_status_by_id,
        },
    )


def bootstrap_repo_seed_workflow_bundle(
    *,
    asset_path: str | Path,
    publish_context_manager_factory: Callable[[], Any] | None = None,
    force_republish: bool = False,
    target_workflow_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Publish and validate one repo-side workflow seed bundle.

    Repo-side bundles are startup seed fixtures only. Canonical runtime
    workflow authority remains in Vontology; this helper only verifies whether
    Vontology is already current and, when it is not, republishes the missing or
    drifted materialisation through the shared publication pathway.  When a
    bundle declares a seed version, the repo seed may publish only workflows
    whose declared version is higher than the version marker already recorded in
    Vontology.
    """

    bundle = authority_service.load_repo_seed_workflow_bundle(asset_path)
    family_id = str(bundle.get("family_id") or "").strip() or None
    managed_by = str(bundle.get("managed_by") or "").strip() or None
    seed_version = _normalise_seed_version(bundle.get("seed_version"))
    source_tag = str(bundle.get("source_tag") or "").strip() or None
    support_concept_report: dict[str, Any] = {
        "created_concept_ids": [],
        "existing_concept_ids": [],
        "relationship_updated_concept_ids": [],
        "text_relation_updated_concept_ids": [],
        "errors": [],
        "skipped": True,
        "skip_reason": "no_authorised_workflow_publication",
    }
    publication_specs = dict(bundle.get("publication_specs") or {})
    publication_purposes = dict(bundle.get("publication_purposes") or {})
    workflow_type_ids = dict(bundle.get("workflow_type_ids") or {})
    workflow_text_relations = dict(bundle.get("workflow_text_relations") or {})
    workflow_launch_input_contracts = dict(
        bundle.get("workflow_launch_input_contracts")
        or bundle.get("workflow_launch_contracts")
        or {}
    )
    step_text_relations = dict(bundle.get("step_text_relations") or {})
    supported_action_ids = tuple(bundle.get("supported_action_ids") or ())
    requested_workflow_ids = tuple(
        str(workflow_id).strip()
        for workflow_id in (target_workflow_ids or ())
        if isinstance(workflow_id, str) and str(workflow_id).strip()
    )
    if requested_workflow_ids:
        allowed_ids = set(requested_workflow_ids)
        publication_specs = {
            workflow_id: spec
            for workflow_id, spec in publication_specs.items()
            if workflow_id in allowed_ids
        }
        publication_purposes = {
            workflow_id: purpose
            for workflow_id, purpose in publication_purposes.items()
            if workflow_id in allowed_ids
        }
        workflow_type_ids = {
            workflow_id: type_ids
            for workflow_id, type_ids in workflow_type_ids.items()
            if workflow_id in allowed_ids
        }
        workflow_text_relations = {
            workflow_id: relation_specs
            for workflow_id, relation_specs in workflow_text_relations.items()
            if workflow_id in allowed_ids
        }
        workflow_launch_input_contracts = {
            workflow_id: contract
            for workflow_id, contract in workflow_launch_input_contracts.items()
            if workflow_id in allowed_ids
        }
    target_workflow_ids = tuple(publication_specs.keys())
    authority_contract = {
        "canonical_source": "vontology",
        "repo_seed_role": "startup_seed_publication_and_repair_only",
        "request_path_dependency_allowed": False,
    }
    repo_seed_version_status_by_id = _repo_seed_version_status_by_workflow(
        seed_version=seed_version,
        workflow_ids=target_workflow_ids,
    )
    repo_seed_version_blocked_ids = tuple(
        workflow_id
        for workflow_id, status in repo_seed_version_status_by_id.items()
        if bool(status.get("blocked")) and not force_republish
    )
    repo_seed_version_refresh_ids = tuple(
        workflow_id
        for workflow_id, status in repo_seed_version_status_by_id.items()
        if status.get("comparison") == 1 and not force_republish
    )
    already_current, existing_validation_by_workflow_id, materialisation_preflight = (
        _validate_existing_materialisation(
            target_workflow_ids=target_workflow_ids,
            supported_action_ids=supported_action_ids,
            publication_specs=publication_specs,
            workflow_text_relations=workflow_text_relations,
            workflow_launch_input_contracts=workflow_launch_input_contracts,
        )
    )
    known_legacy_digests_by_workflow = dict(
        bundle.get("known_legacy_authority_payload_sha256_by_seed_version") or {}
    )
    target_authority_payload_sha256_by_workflow: dict[str, str] = {}
    target_authority_payload_by_workflow: dict[str, dict[str, Any]] = {}
    observed_authority_payload_by_workflow: dict[str, dict[str, Any] | None] = {}
    observed_authority_payload_sha256_by_workflow: dict[str, str | None] = {}
    authority_adjudication_by_workflow: dict[str, dict[str, Any]] = {}
    publication_workflow_ids: list[str] = []
    marker_only_workflow_ids: list[str] = []
    migration_source_payload_by_workflow: dict[str, dict[str, Any]] = {}
    migration_source_absent_workflow_ids: set[str] = set()
    authority_blockers_by_workflow: dict[str, dict[str, Any]] = {}

    def _step_relation_scope(workflow_id: str, spec: Any) -> dict[str, tuple[dict[str, Any], ...]]:
        step_ids = set(
            authority_service.publication_spec_step_concept_ids(
                workflow_id=workflow_id,
                spec=spec,
            )
        )
        return {
            step_id: tuple(step_text_relations.get(step_id) or ())
            for step_id in sorted(step_ids)
            if step_text_relations.get(step_id)
        }

    for workflow_id in target_workflow_ids:
        spec = publication_specs[workflow_id]
        scoped_types = tuple(workflow_type_ids.get(workflow_id) or ())
        scoped_relations = tuple(workflow_text_relations.get(workflow_id) or ())
        scoped_contract = workflow_launch_input_contracts.get(workflow_id)
        scoped_step_relations = _step_relation_scope(workflow_id, spec)
        target_payload = _workflow_seed_authority_payload_from_bundle_surfaces(
            workflow_id=workflow_id,
            publication_spec=spec,
            source_workflow_type_ids=scoped_types,
            scoped_workflow_type_ids=scoped_types,
            source_workflow_text_relations=scoped_relations,
            scoped_workflow_text_relations=scoped_relations,
            source_launch_input_contract=(
                scoped_contract if isinstance(scoped_contract, Mapping) else None
            ),
            source_step_text_relations=scoped_step_relations,
            scoped_step_text_relations=scoped_step_relations,
        )
        target_sha256 = _stable_payload_sha256(target_payload)
        target_authority_payload_by_workflow[workflow_id] = target_payload
        target_authority_payload_sha256_by_workflow[workflow_id] = target_sha256
        live_authority_read_error: str | None = None
        try:
            live_payload = _load_live_workflow_seed_authority_payload(
                workflow_id=workflow_id,
                scoped_workflow_type_ids=scoped_types,
                scoped_workflow_text_relations=scoped_relations,
                scoped_launch_input_contract=(
                    scoped_contract if isinstance(scoped_contract, Mapping) else None
                ),
                scoped_step_text_relations=scoped_step_relations,
            )
            if live_payload is None:
                live_payload = _load_live_workflow_seed_partial_authority_payload(
                    workflow_id=workflow_id,
                    scoped_workflow_type_ids=scoped_types,
                    scoped_workflow_text_relations=scoped_relations,
                    scoped_launch_input_contract=(
                        scoped_contract
                        if isinstance(scoped_contract, Mapping)
                        else None
                    ),
                    scoped_step_text_relations=scoped_step_relations,
                )
        except Exception as exc:
            live_payload = None
            live_authority_read_error = str(exc)
        live_sha256 = (
            _stable_payload_sha256(live_payload)
            if isinstance(live_payload, Mapping)
            else None
        )
        observed_authority_payload_by_workflow[workflow_id] = (
            dict(live_payload) if isinstance(live_payload, Mapping) else None
        )
        observed_authority_payload_sha256_by_workflow[workflow_id] = live_sha256
        version_status = repo_seed_version_status_by_id.get(workflow_id) or {}
        marker = version_status.get("marker")
        marker = marker if isinstance(marker, Mapping) else {}
        existing_version = _normalise_seed_version(
            version_status.get("vontology_seed_version")
        )
        version_key = (existing_version or "unversioned").lower()
        marker_sha256 = str(marker.get("authority_payload_sha256") or "").strip().lower()
        raw_known_versions = known_legacy_digests_by_workflow.get(workflow_id)
        known_versions = raw_known_versions if isinstance(raw_known_versions, Mapping) else {}
        known_digests = {
            str(item).strip().lower()
            for item in (known_versions.get(version_key) or ())
            if isinstance(item, str) and str(item).strip()
        }
        exact_reviewed_source = bool(
            live_sha256
            and (
                live_sha256 == marker_sha256
                or live_sha256 in known_digests
            )
        )
        pending_receipt = marker.get("migration_receipt")
        pending_receipt = (
            pending_receipt if isinstance(pending_receipt, Mapping) else {}
        )
        pending_source_payload = pending_receipt.get("source_authority_payload")
        pending_source_sha256 = str(
            pending_receipt.get("source_authority_payload_sha256") or ""
        ).strip().lower()
        pending_source_version = str(
            pending_receipt.get("source_seed_version") or "unversioned"
        ).strip().lower()
        pending_known_digests = {
            str(item).strip().lower()
            for item in (known_versions.get(pending_source_version) or ())
            if isinstance(item, str) and str(item).strip()
        }
        pending_source_absent = pending_receipt.get("source_authority_absent") is True
        pending_current_payload: Mapping[str, Any] | None = (
            live_payload
            if isinstance(live_payload, Mapping)
            else (
                {}
                if live_authority_read_error is None and pending_source_absent
                else None
            )
        )
        pending_source_is_reviewed = bool(
            isinstance(pending_source_payload, Mapping)
            and (
                (
                    pending_source_absent
                    and not pending_source_payload
                    and pending_source_sha256 == _stable_payload_sha256({})
                    and pending_source_version == "unversioned"
                )
                or (
                    not pending_source_absent
                    and pending_source_sha256 in pending_known_digests
                )
            )
        )
        pending_receipt_valid = bool(
            pending_current_payload is not None
            and isinstance(pending_source_payload, Mapping)
            and pending_receipt.get("schema_version")
            == _REPO_SEED_MIGRATION_RECEIPT_SCHEMA_VERSION
            and pending_receipt.get("status") == "pending"
            and str(pending_receipt.get("target_seed_version") or "").strip()
            == seed_version
            and str(
                pending_receipt.get("target_authority_payload_sha256") or ""
            ).strip().lower()
            == target_sha256
            and _stable_payload_sha256(pending_source_payload)
            == pending_source_sha256
            and pending_source_is_reviewed
            and _authority_payload_is_source_target_partial(
                source_payload=(
                    None if pending_source_absent else pending_source_payload
                ),
                target_payload=target_payload,
                current_payload=pending_current_payload,
            )
        )
        comparison = version_status.get("comparison")
        workflow_status = (
            (materialisation_preflight.get("workflow_status_by_id") or {}).get(
                workflow_id
            )
            or {}
        )
        reason: str
        if force_republish:
            publication_workflow_ids.append(workflow_id)
            reason = "forced_republish"
        elif live_authority_read_error is not None:
            reason = "workflow_seed_live_authority_read_failed"
            authority_blockers_by_workflow[workflow_id] = {
                "error_code": reason,
                "error": live_authority_read_error,
                "repo_seed_version": seed_version,
                "target_authority_payload_sha256": target_sha256,
            }
        elif live_sha256 == target_sha256:
            reason = "target_authority_already_exact"
            if comparison == 1 or existing_version is None:
                marker_only_workflow_ids.append(workflow_id)
        elif pending_receipt_valid and isinstance(pending_source_payload, Mapping):
            publication_workflow_ids.append(workflow_id)
            migration_source_payload_by_workflow[workflow_id] = {
                str(key): value for key, value in pending_source_payload.items()
            }
            if pending_source_absent:
                migration_source_absent_workflow_ids.add(workflow_id)
            reason = "pending_reviewed_migration_resume"
        elif live_sha256 is None and existing_version is None and str(
            workflow_status.get("status") or ""
        ) in {"graph_missing", "definition_missing"}:
            publication_workflow_ids.append(workflow_id)
            migration_source_payload_by_workflow[workflow_id] = {}
            migration_source_absent_workflow_ids.add(workflow_id)
            reason = "safe_first_publication"
        elif (
            comparison == 1
            and exact_reviewed_source
            and isinstance(live_payload, Mapping)
        ):
            publication_workflow_ids.append(workflow_id)
            migration_source_payload_by_workflow[workflow_id] = {
                str(key): value for key, value in live_payload.items()
            }
            reason = "exact_reviewed_legacy_migration"
        elif (
            existing_version is None
            and exact_reviewed_source
            and isinstance(live_payload, Mapping)
        ):
            publication_workflow_ids.append(workflow_id)
            migration_source_payload_by_workflow[workflow_id] = {
                str(key): value for key, value in live_payload.items()
            }
            reason = "exact_reviewed_unversioned_migration"
        else:
            reason = "live_authority_requires_explicit_migration"
            authority_blockers_by_workflow[workflow_id] = {
                "error_code": reason,
                "observed_seed_version": existing_version,
                "repo_seed_version": seed_version,
                "observed_authority_payload_sha256": live_sha256,
                "target_authority_payload_sha256": target_sha256,
                "marker_authority_payload_sha256": marker_sha256 or None,
                "known_legacy_authority_payload_sha256": sorted(known_digests),
            }
        authority_adjudication_by_workflow[workflow_id] = {
            "workflow_id": workflow_id,
            "reason": reason,
            "publication_authorised": workflow_id in publication_workflow_ids,
            "marker_only_update_authorised": workflow_id in marker_only_workflow_ids,
            "observed_authority_payload_sha256": live_sha256,
            "target_authority_payload_sha256": target_sha256,
            "exact_reviewed_source": exact_reviewed_source,
        }

    for workflow_id, source_payload in migration_source_payload_by_workflow.items():
        adjudication = authority_adjudication_by_workflow.get(workflow_id) or {}
        if adjudication.get("reason") == "pending_reviewed_migration_resume":
            continue
        version_status = repo_seed_version_status_by_id.get(workflow_id) or {}
        try:
            _upsert_workflow_repo_seed_pending_migration_receipt(
                workflow_id=workflow_id,
                source_seed_version=_normalise_seed_version(
                    version_status.get("vontology_seed_version")
                ),
                source_authority_payload=source_payload,
                source_authority_payload_sha256=_stable_payload_sha256(
                    source_payload
                ),
                source_authority_absent=(
                    workflow_id in migration_source_absent_workflow_ids
                ),
                target_seed_version=seed_version or "",
                target_authority_payload_sha256=(
                    target_authority_payload_sha256_by_workflow[workflow_id]
                ),
                family_id=family_id,
                source_tag=source_tag,
                managed_by=managed_by,
                asset_path=str(bundle.get("asset_path") or Path(asset_path)),
            )
        except Exception as exc:
            publication_workflow_ids.remove(workflow_id)
            authority_blockers_by_workflow[workflow_id] = {
                "error_code": "workflow_seed_migration_receipt_upsert_failed",
                "error": str(exc),
            }
            authority_adjudication_by_workflow[workflow_id][
                "publication_authorised"
            ] = False
            authority_adjudication_by_workflow[workflow_id]["reason"] = (
                "workflow_seed_migration_receipt_upsert_failed"
            )

    support_candidate_workflow_ids = [
        workflow_id
        for workflow_id in target_workflow_ids
        if workflow_id not in authority_blockers_by_workflow
        and (
            force_republish
            or (repo_seed_version_status_by_id.get(workflow_id) or {}).get(
                "comparison"
            )
            != -1
        )
    ]
    if support_candidate_workflow_ids:
        support_concept_specs = bundle.get("support_concepts")
        if requested_workflow_ids:
            support_concept_specs = _scope_support_concepts_to_authority_payloads(
                support_concept_specs,
                {
                    workflow_id: target_authority_payload_by_workflow[workflow_id]
                    for workflow_id in support_candidate_workflow_ids
                },
            )
        with suspend_event_workflow_integration():
            support_concept_report = _materialise_support_concepts(
                support_concept_specs,
                source_tag=source_tag,
                managed_by=managed_by,
                update_existing=False,
            )
        support_concept_report["skipped"] = False
        support_concept_report["skip_reason"] = None
        support_errors = list(support_concept_report.get("errors") or ())
        if support_errors:
            for workflow_id in support_candidate_workflow_ids:
                if workflow_id in publication_workflow_ids:
                    publication_workflow_ids.remove(workflow_id)
                if workflow_id in marker_only_workflow_ids:
                    marker_only_workflow_ids.remove(workflow_id)
                authority_blockers_by_workflow[workflow_id] = {
                    "error_code": (
                        "workflow_seed_support_concept_materialisation_failed"
                    ),
                    "support_concept_errors": copy.deepcopy(support_errors),
                }
                authority_adjudication_by_workflow[workflow_id][
                    "publication_authorised"
                ] = False
                authority_adjudication_by_workflow[workflow_id][
                    "marker_only_update_authorised"
                ] = False
                authority_adjudication_by_workflow[workflow_id]["reason"] = (
                    "workflow_seed_support_concept_materialisation_failed"
                )

    publication_workflow_ids_tuple = tuple(publication_workflow_ids)
    publication_specs_to_apply = {
        workflow_id: publication_specs[workflow_id]
        for workflow_id in publication_workflow_ids_tuple
    }
    publication_purposes_to_apply = {
        workflow_id: purpose
        for workflow_id, purpose in publication_purposes.items()
        if workflow_id in publication_specs_to_apply
    }
    materialisation_preflight["repo_seed_version_gate"] = {
        "seed_version": seed_version,
        "version_marker_predicate": _REPO_SEED_VERSION_TEXT_PREDICATE,
        "blocked_workflow_ids": list(repo_seed_version_blocked_ids),
        "refresh_workflow_ids": list(repo_seed_version_refresh_ids),
        "status_by_workflow_id": repo_seed_version_status_by_id,
        "authority_adjudication_by_workflow": authority_adjudication_by_workflow,
        "authority_blockers_by_workflow": authority_blockers_by_workflow,
        "publication_workflow_ids": list(publication_workflow_ids_tuple),
        "marker_only_workflow_ids": list(marker_only_workflow_ids),
        "forced_republish": bool(force_republish),
    }
    skip_publication = not publication_workflow_ids_tuple
    no_op_version_blocked_ids = tuple(
        workflow_id
        for workflow_id in repo_seed_version_blocked_ids
        if workflow_id not in publication_workflow_ids_tuple
    )

    typed_workflow_ids: list[str] = []
    typed_step_ids: list[str] = []
    seed_version_marker_updates: list[dict[str, Any]] = []
    validation_by_workflow_id: dict[str, dict[str, Any]] = {}
    postpublication_definition_cache: dict[str, Any | None] = {}

    def _postpublication_cached_loader(candidate_workflow_id: str) -> Any | None:
        workflow_id = str(candidate_workflow_id or "").strip()
        if not workflow_id:
            return None
        if workflow_id not in postpublication_definition_cache:
            postpublication_definition_cache[workflow_id] = (
                load_workflow_definition_from_vontology(workflow_id)
            )
        return postpublication_definition_cache[workflow_id]

    if skip_publication:
        validation_by_workflow_id.update(existing_validation_by_workflow_id)

    context_manager_factory = (
        publish_context_manager_factory or suspend_event_workflow_integration
    )
    with context_manager_factory():
        publication_report: dict[str, Any]
        if skip_publication:
            publication_report = {
                "counts": {
                    "workflows_targeted": len(target_workflow_ids),
                    "workflows_published": 0,
                    "workflows_skipped_missing_registration": 0,
                    "workflows_skipped_missing_concept": 0,
                    "step_concepts_created": 0,
                    "action_concepts_created": 0,
                    "mapping_concepts_created": 0,
                    "validation_failures": 0,
                    "errors": len(authority_blockers_by_workflow),
                },
                "published_workflow_ids": [],
                "skipped_due_to_current_materialisation": [
                    workflow_id
                    for workflow_id in target_workflow_ids
                    if workflow_id not in authority_blockers_by_workflow
                ],
                "errors_by_workflow_id": {
                    workflow_id: blocker["error_code"]
                    for workflow_id, blocker in authority_blockers_by_workflow.items()
                },
                "skip_reason": (
                    "workflow_seed_authority_blocked"
                    if authority_blockers_by_workflow
                    else "existing_materialisation_valid"
                ),
                "skipped": True,
                "forced_republish": False,
            }
        else:
            publication_report = authority_service.publish_canonical_chat_workflow_graphs(
                target_workflow_ids=publication_workflow_ids_tuple,
                upsert_publication_lifecycle_metadata=True,
                publication_specs=publication_specs_to_apply,
                publication_definitions=authority_service._build_definition_map_from_publication_specs(
                    publication_specs_to_apply
                ),
                publication_purposes=publication_purposes_to_apply,
                validate_after_publish=False,
            )
            publication_report["forced_republish"] = bool(force_republish)
            publication_report.setdefault("errors_by_workflow_id", {}).update(
                {
                    workflow_id: blocker["error_code"]
                    for workflow_id, blocker in authority_blockers_by_workflow.items()
                }
            )
            publication_report.setdefault("counts", {})["errors"] = int(
                (publication_report.get("counts") or {}).get("errors") or 0
            ) + len(authority_blockers_by_workflow)
        publication_report["materialisation_status"] = (
            "current"
            if skip_publication
            else (
                "forced_republish"
                if force_republish
                and not materialisation_preflight.get("drift_detected")
                else (
                    "repo_seed_version_refresh"
                    if repo_seed_version_refresh_ids
                    and not materialisation_preflight.get("drift_detected")
                    else "repaired_from_repo_seed"
                )
            )
        )
        publication_report["drift_detected"] = bool(
            materialisation_preflight.get("drift_detected")
        )
        publication_report["current_workflow_ids"] = list(
            materialisation_preflight.get("current_workflow_ids") or []
        )
        publication_report["drift_workflow_ids"] = list(
            materialisation_preflight.get("drift_workflow_ids") or []
        )
        publication_report["issue_codes"] = list(
            materialisation_preflight.get("issue_codes") or []
        )
        publication_report["bundle_snapshot_drift_detected"] = bool(
            materialisation_preflight.get("bundle_snapshot_drift_detected")
        )
        publication_report["bundle_snapshot_drift_workflow_ids"] = list(
            materialisation_preflight.get("bundle_snapshot_drift_workflow_ids") or []
        )
        publication_report["bundle_snapshot_issue_codes"] = list(
            materialisation_preflight.get("bundle_snapshot_issue_codes") or []
        )
        publication_report[
            "repo_seed_version_suppressed_bundle_snapshot_drift_workflow_ids"
        ] = list(
            materialisation_preflight.get(
                "repo_seed_version_suppressed_bundle_snapshot_drift_workflow_ids"
            )
            or []
        )
        publication_report["repo_seed_version_gate"] = materialisation_preflight[
            "repo_seed_version_gate"
        ]
        publication_report["repo_seed_version_refresh_workflow_ids"] = list(
            repo_seed_version_refresh_ids
        )
        publication_report["skipped_due_to_seed_version_not_newer"] = list(
            no_op_version_blocked_ids
        )

        should_apply_seed_bundle_mutations = bool(publication_workflow_ids_tuple)
        if should_apply_seed_bundle_mutations:
            for workflow_id, spec in publication_specs_to_apply.items():
                type_ids = tuple(workflow_type_ids.get(workflow_id) or ())
                if type_ids and ensure_instance_typing(
                    concept_id=workflow_id,
                    type_ids=type_ids,
                    remove_type_parent_ids=type_ids,
                ):
                    typed_workflow_ids.append(workflow_id)

                relation_specs = tuple(workflow_text_relations.get(workflow_id) or ())
                if relation_specs:
                    authority_service.upsert_seed_bundle_text_relations(
                        subject_concept_id=workflow_id,
                        relation_specs=relation_specs,
                        workflow_id=workflow_id,
                        source_tag=source_tag,
                        managed_by=managed_by,
                    )

                launch_input_contract = workflow_launch_input_contracts.get(workflow_id)
                if isinstance(launch_input_contract, dict):
                    upsert_singleton_text_relation(
                        subject_concept_id=workflow_id,
                        predicate="#V#hasWorkflowLaunchInputContractJson",
                        text=json.dumps(
                            launch_input_contract,
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                        lang="en-NZ",
                        context={
                            "workflow_id": workflow_id,
                            **({"source": source_tag} if source_tag else {}),
                            **({"managed_by": managed_by} if managed_by else {}),
                        },
                        garbage_collect=True,
                    )

                for step, step_concept_id in zip(
                    spec.steps,
                    authority_service.publication_spec_step_concept_ids(
                        workflow_id=workflow_id,
                        spec=spec,
                    ),
                ):
                    if ensure_instance_typing(
                        concept_id=step_concept_id,
                        type_ids=(_WORKFLOW_STEP_TYPE_ID,),
                    ):
                        typed_step_ids.append(step_concept_id)
                    step_relation_specs = tuple(
                        step_text_relations.get(step_concept_id) or ()
                    )
                    if step_relation_specs:
                        authority_service.upsert_seed_bundle_text_relations(
                            subject_concept_id=step_concept_id,
                            relation_specs=step_relation_specs,
                            workflow_id=workflow_id,
                            source_tag=source_tag,
                            managed_by=managed_by,
                            state_id=(
                                str(getattr(step, "state_id", "") or "").strip() or None
                            ),
                        )

        for workflow_id, spec in publication_specs_to_apply.items():
            validation = validation_by_workflow_id.get(workflow_id)
            if skip_publication:
                if not isinstance(validation, dict):
                    raise RuntimeError(
                        "repo_seed_workflow_validation_missing_after_short_circuit:"
                        f"{workflow_id}"
                    )
            else:
                graph, graph_warnings = build_workflow_process_graph(workflow_id)
                if not isinstance(graph, dict):
                    raise RuntimeError(
                        f"repo_seed_workflow_graph_missing:{workflow_id}"
                    )
                warning_items = [
                    str(item).strip()
                    for item in (graph_warnings or [])
                    if isinstance(item, str) and str(item).strip()
                ]
                if warning_items:
                    raise RuntimeError(
                        "repo_seed_workflow_graph_warnings_present:"
                        f"{workflow_id}:" + ",".join(warning_items)
                    )

                definition = _postpublication_cached_loader(workflow_id)
                if definition is None:
                    raise RuntimeError(
                        f"repo_seed_workflow_definition_not_loadable:{workflow_id}"
                    )
                validation = validate_workflow_definition_contract(
                    definition=definition,
                    supported_action_ids=supported_action_ids,
                    known_workflow_ids=target_workflow_ids,
                    workflow_definition_loader=_postpublication_cached_loader,
                )
                validation_by_workflow_id[workflow_id] = copy.deepcopy(validation)

                if not bool(validation.get("valid")):
                    raise RuntimeError(
                        "repo_seed_workflow_definition_invalid:"
                        f"{workflow_id}:"
                        + ",".join(
                            str(item).strip()
                            for item in (validation.get("errors") or [])
                            if isinstance(item, str) and str(item).strip()
                        )
                    )

        readback_failures_by_workflow: dict[str, dict[str, Any]] = {}
        for workflow_id in publication_workflow_ids_tuple:
            spec = publication_specs[workflow_id]
            scoped_contract = workflow_launch_input_contracts.get(workflow_id)
            try:
                readback_payload = _load_live_workflow_seed_authority_payload(
                    workflow_id=workflow_id,
                    scoped_workflow_type_ids=tuple(
                        workflow_type_ids.get(workflow_id) or ()
                    ),
                    scoped_workflow_text_relations=tuple(
                        workflow_text_relations.get(workflow_id) or ()
                    ),
                    scoped_launch_input_contract=(
                        scoped_contract
                        if isinstance(scoped_contract, Mapping)
                        else None
                    ),
                    scoped_step_text_relations=_step_relation_scope(
                        workflow_id,
                        spec,
                    ),
                )
            except Exception:
                readback_payload = None
            observed_readback_sha256 = (
                _stable_payload_sha256(readback_payload)
                if isinstance(readback_payload, Mapping)
                else None
            )
            expected_readback_sha256 = (
                target_authority_payload_sha256_by_workflow[workflow_id]
            )
            if observed_readback_sha256 != expected_readback_sha256:
                expected_payload = target_authority_payload_by_workflow[workflow_id]
                observed_payload = (
                    readback_payload if isinstance(readback_payload, Mapping) else {}
                )
                readback_failures_by_workflow[workflow_id] = {
                    "error_code": "workflow_seed_authority_readback_mismatch",
                    "expected_authority_payload_sha256": expected_readback_sha256,
                    "observed_authority_payload_sha256": observed_readback_sha256,
                    "mismatched_authority_surfaces": [
                        key
                        for key in expected_payload
                        if expected_payload.get(key) != observed_payload.get(key)
                    ],
                    "first_mismatch_path": _first_payload_mismatch_path(
                        _normalise_workflow_seed_authority_payload(expected_payload),
                        _normalise_workflow_seed_authority_payload(observed_payload),
                    ),
                }
        if readback_failures_by_workflow:
            publication_report.setdefault("errors_by_workflow_id", {}).update(
                {
                    workflow_id: failure["error_code"]
                    for workflow_id, failure in readback_failures_by_workflow.items()
                }
            )
            publication_report.setdefault("counts", {})["errors"] = int(
                (publication_report.get("counts") or {}).get("errors") or 0
            ) + len(readback_failures_by_workflow)
        publication_report["authority_readback_failures_by_workflow"] = (
            readback_failures_by_workflow
        )

        if seed_version:
            marker_update_workflow_ids = tuple(
                dict.fromkeys(
                    [
                        *marker_only_workflow_ids,
                        *[
                            workflow_id
                            for workflow_id in publication_workflow_ids_tuple
                            if workflow_id not in readback_failures_by_workflow
                        ],
                    ]
                )
            )
            for workflow_id in marker_update_workflow_ids:
                update = _upsert_workflow_repo_seed_version_marker(
                    workflow_id=workflow_id,
                    seed_version=seed_version,
                    family_id=family_id,
                    source_tag=source_tag,
                    managed_by=managed_by,
                    asset_path=str(bundle.get("asset_path") or Path(asset_path)),
                    authority_payload_sha256=(
                        target_authority_payload_sha256_by_workflow[workflow_id]
                    ),
                )
                seed_version_marker_updates.append(
                    {
                        "workflow_id": workflow_id,
                        "relation_created": bool(update.get("relation_created")),
                        "replaced_count": int(update.get("replaced_count") or 0),
                    }
                )

    if should_apply_seed_bundle_mutations:
        invalidate_workflow_discovery_executability_caches()
    return {
        "asset_path": str(bundle.get("asset_path") or Path(asset_path)),
        "family_id": bundle.get("family_id"),
        "authority_contract": authority_contract,
        "materialisation_preflight": materialisation_preflight,
        "workflow_ids": list(target_workflow_ids),
        "repo_seed_version_gate": materialisation_preflight["repo_seed_version_gate"],
        "seed_version_marker_updates": seed_version_marker_updates,
        "publication": publication_report,
        "typed_workflow_ids": typed_workflow_ids,
        "typed_step_ids": typed_step_ids,
        "support_concepts": support_concept_report,
        "validation_by_workflow_id": validation_by_workflow_id,
    }


__all__ = ["bootstrap_repo_seed_workflow_bundle"]
