"""Apply the concept-summary field Vontology seed bundle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_summary_field_resolver import get_concept_summary_field_resolver
from .text_value_service import upsert_singleton_text_relation

SUMMARY_FIELD_SEED_SCHEMA_VERSION = "concept_summary_field_seed_bundle.v1"
SUMMARY_FIELD_TYPE_ID = "#V#summary_field"

_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "vontology"
    / "seed_bundles"
    / "concept_summary_fields_seed_bundle.json"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _load_seed_bundle(asset_path: str | Path | None) -> Mapping[str, Any]:
    path = Path(asset_path or _SEED_ASSET_PATH).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("concept_summary_field_seed_bundle_not_mapping")
    if _text(payload.get("schema_version")) != SUMMARY_FIELD_SEED_SCHEMA_VERSION:
        raise ValueError("concept_summary_field_seed_bundle_schema_unsupported")
    return {**payload, "asset_path": str(path)}


def _concept_exists(concept_id: str) -> bool:
    try:
        return isinstance(
            concept_service.get_concept_by_concept_id_exact(concept_id),
            Mapping,
        )
    except Exception:
        return False


def _ensure_concept(spec: Mapping[str, Any], *, source_tag: str, managed_by: str) -> str:
    concept_id = _text(spec.get("concept_id"))
    name = _text(spec.get("name"))
    if not concept_id or not name:
        raise ValueError("concept_identity_missing")
    if _concept_exists(concept_id):
        return concept_id
    attributes = dict(spec.get("attributes") or {})
    attributes.setdefault("repo_seed_source_tag", source_tag)
    attributes.setdefault("repo_seed_managed_by", managed_by)
    concept_service.create_concept(
        name=name,
        concept_id=concept_id,
        parent_concept_ids=_strings(spec.get("parent_concept_ids")),
        create_as_instance=bool(spec.get("create_as_instance")),
        description=spec.get("description"),
        notes=spec.get("notes"),
        system_tags=_strings(spec.get("system_tags")),
        attributes=attributes,
        visibility_scope_mode="global_general",
    )
    return concept_id


def _upsert_seed_text_relation(
    spec: Mapping[str, Any],
    *,
    source_tag: str,
    managed_by: str,
) -> str:
    subject_id = _text(spec.get("subject_concept_id"))
    predicate = _text(spec.get("predicate"))
    text = _text(spec.get("text"))
    if not text and "text_json" in spec:
        text = json.dumps(_strings(spec.get("text_json")), ensure_ascii=True)
    if not subject_id or not predicate or not text:
        raise ValueError("text_relation_identity_missing")
    upsert_singleton_text_relation(
        subject_concept_id=subject_id,
        predicate=predicate,
        text=text,
        lang=_text(spec.get("lang")) or "en-NZ",
        context={
            "schema_version": SUMMARY_FIELD_SEED_SCHEMA_VERSION,
            "source": source_tag,
            "managed_by": managed_by,
        },
        garbage_collect=True,
    )
    return f"{subject_id}:{predicate}"


def _merge_relationship(spec: Mapping[str, Any]) -> str:
    subject_id = _text(spec.get("subject_concept_id"))
    predicate = _text(spec.get("predicate"))
    targets = _strings(spec.get("object_concept_ids"))
    if not subject_id or not predicate or not targets:
        raise ValueError("relationship_identity_missing")
    concept = concept_service.get_concept_by_concept_id(subject_id)
    if not isinstance(concept, Mapping):
        raise ValueError(f"relationship_subject_missing:{subject_id}")
    relationships = dict(concept.get("relationships") or {})
    existing = _strings(relationships.get(predicate))
    updated = _strings([*existing, *targets])
    if updated != existing:
        concept_service.update_concept(subject_id, {f"relationships.{predicate}": updated})
    return f"{subject_id}:{predicate}"


def bootstrap_canonical_concept_summary_fields(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Materialise canonical concept-summary field metadata into Vontology."""

    bundle = _load_seed_bundle(asset_path)
    source_tag = _text(bundle.get("source_tag")) or "JVNAUTOSCI-1958"
    managed_by = (
        _text(bundle.get("managed_by")) or "concept_summary_field_vontology_service"
    )
    errors: list[dict[str, str]] = []

    def apply_many(section: str, callback) -> list[str]:
        applied: list[str] = []
        for spec in bundle.get(section) or ():
            if not isinstance(spec, Mapping):
                continue
            identity = _text(
                spec.get("concept_id")
                or spec.get("subject_concept_id")
                or spec.get("predicate")
            )
            try:
                applied.append(callback(spec))
            except Exception as exc:
                errors.append(
                    {"section": section, "id": identity, "reason_code": str(exc)}
                )
        return applied

    concept_ids = apply_many(
        "concepts",
        lambda spec: _ensure_concept(
            spec,
            source_tag=source_tag,
            managed_by=managed_by,
        ),
    )
    text_relation_ids = apply_many(
        "text_relations",
        lambda spec: _upsert_seed_text_relation(
            spec,
            source_tag=source_tag,
            managed_by=managed_by,
        ),
    )
    relationship_ids = apply_many("relationships", _merge_relationship)
    field_concept_ids = [
        concept_id
        for spec in bundle.get("concepts") or ()
        if isinstance(spec, Mapping)
        and SUMMARY_FIELD_TYPE_ID in _strings(spec.get("parent_concept_ids"))
        for concept_id in [_text(spec.get("concept_id"))]
        if concept_id in concept_ids
    ]

    get_concept_summary_field_resolver().invalidate_cache()
    return {
        "success": not errors,
        "asset_path": bundle.get("asset_path"),
        "schema_version": bundle.get("schema_version"),
        "seed_version": bundle.get("seed_version"),
        "source_tag": source_tag,
        "managed_by": managed_by,
        "concept_ids": concept_ids,
        "field_concept_ids": field_concept_ids,
        "text_relation_ids": text_relation_ids,
        "relationship_ids": relationship_ids,
        "errors": errors,
        "counts": {
            "concepts_seen": len(concept_ids),
            "fields_seen": len(field_concept_ids),
            "field_configs_written": len(text_relation_ids),
            "type_bindings_written": len(relationship_ids),
            "errors": len(errors),
        },
    }


__all__ = [
    "SUMMARY_FIELD_SEED_SCHEMA_VERSION",
    "SUMMARY_FIELD_TYPE_ID",
    "bootstrap_canonical_concept_summary_fields",
]
