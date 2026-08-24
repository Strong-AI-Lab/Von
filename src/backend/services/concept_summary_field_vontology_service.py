"""Apply the concept-summary field Vontology seed bundle."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import concept_service
from . import startup_seed_freshness_service as seed_freshness
from .concept_summary_field_resolver import get_concept_summary_field_resolver
from .text_value_service import get_texts_for_concepts, upsert_singleton_text_relation

SUMMARY_FIELD_SEED_SCHEMA_VERSION = "concept_summary_field_seed_bundle.v1"
SUMMARY_FIELD_TYPE_ID = "#V#summary_field"
SUMMARY_FIELD_STARTUP_FAMILY_ID = "concept_summary_fields"
SUMMARY_FIELD_STARTUP_PRODUCER_SCHEMA_VERSION = (
    "concept_summary_field_startup_materialisation.v1"
)

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


def _startup_freshness_scope(bundle: Mapping[str, Any]) -> dict[str, list[str]]:
    concept_specs = [
        spec for spec in bundle.get("concepts") or () if isinstance(spec, Mapping)
    ]
    text_specs = [
        spec for spec in bundle.get("text_relations") or () if isinstance(spec, Mapping)
    ]
    relationship_specs = [
        spec for spec in bundle.get("relationships") or () if isinstance(spec, Mapping)
    ]
    return {
        "concept_ids": _strings(
            [
                *[_text(spec.get("concept_id")) for spec in concept_specs],
                *[_text(spec.get("subject_concept_id")) for spec in relationship_specs],
            ]
        ),
        "text_relation_subject_ids": _strings(
            [_text(spec.get("subject_concept_id")) for spec in text_specs]
        ),
        "text_relation_predicates": _strings(
            [_text(spec.get("predicate")) for spec in text_specs]
        ),
    }


def _startup_source_digest(bundle: Mapping[str, Any]) -> str:
    return seed_freshness.build_startup_seed_source_digest(
        asset_path=_text(bundle.get("asset_path")),
        producer_paths=[Path(__file__), Path(seed_freshness.__file__)],
        material_configuration={
            "seed_schema_version": SUMMARY_FIELD_SEED_SCHEMA_VERSION,
            "producer_schema_version": (SUMMARY_FIELD_STARTUP_PRODUCER_SCHEMA_VERSION),
        },
    )


def _ensure_concept(
    spec: Mapping[str, Any],
    *,
    source_tag: str,
    managed_by: str,
    concepts_by_id: dict[str, Mapping[str, Any]],
) -> tuple[str, bool]:
    concept_id = _text(spec.get("concept_id"))
    name = _text(spec.get("name"))
    if not concept_id or not name:
        raise ValueError("concept_identity_missing")
    if concept_id in concepts_by_id:
        return concept_id, False
    attributes = dict(spec.get("attributes") or {})
    attributes.setdefault("repo_seed_source_tag", source_tag)
    attributes.setdefault("repo_seed_managed_by", managed_by)
    created = concept_service.create_concept(
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
    if isinstance(created, Mapping):
        concepts_by_id[concept_id] = dict(created)
    return concept_id, True


def _seed_text(spec: Mapping[str, Any]) -> str:
    text = _text(spec.get("text"))
    if not text and "text_json" in spec:
        text = json.dumps(_strings(spec.get("text_json")), ensure_ascii=True)
    return text


def _singleton_text_is_current(
    rows: Sequence[Mapping[str, Any]],
    *,
    predicate: str,
    text: str,
    lang: str,
) -> bool:
    same_slot = [
        row
        for row in rows
        if _text(row.get("predicate")) == predicate and _text(row.get("lang")) == lang
    ]
    return len(same_slot) == 1 and _text(same_slot[0].get("text")) == text


def _upsert_seed_text_relation(
    spec: Mapping[str, Any],
    *,
    source_tag: str,
    managed_by: str,
    current_rows: Sequence[Mapping[str, Any]],
) -> tuple[str, bool]:
    subject_id = _text(spec.get("subject_concept_id"))
    predicate = _text(spec.get("predicate"))
    text = _seed_text(spec)
    if not subject_id or not predicate or not text:
        raise ValueError("text_relation_identity_missing")
    lang = _text(spec.get("lang")) or "en-NZ"
    identity = f"{subject_id}:{predicate}"
    if _singleton_text_is_current(
        current_rows,
        predicate=predicate,
        text=text,
        lang=lang,
    ):
        return identity, False
    upsert_singleton_text_relation(
        subject_concept_id=subject_id,
        predicate=predicate,
        text=text,
        lang=lang,
        context={
            "schema_version": SUMMARY_FIELD_SEED_SCHEMA_VERSION,
            "source": source_tag,
            "managed_by": managed_by,
        },
        garbage_collect=True,
    )
    return identity, True


def _merge_relationship(
    spec: Mapping[str, Any],
    *,
    concepts_by_id: dict[str, Mapping[str, Any]],
) -> tuple[str, bool]:
    subject_id = _text(spec.get("subject_concept_id"))
    predicate = _text(spec.get("predicate"))
    targets = _strings(spec.get("object_concept_ids"))
    if not subject_id or not predicate or not targets:
        raise ValueError("relationship_identity_missing")
    concept = concepts_by_id.get(subject_id)
    if not isinstance(concept, Mapping):
        raise ValueError(f"relationship_subject_missing:{subject_id}")
    relationships = dict(concept.get("relationships") or {})
    existing = _strings(relationships.get(predicate))
    updated = _strings([*existing, *targets])
    if updated != existing:
        concept_service.update_concept(
            subject_id, {f"relationships.{predicate}": updated}
        )
        refreshed = {
            **dict(concept),
            "relationships": {**relationships, predicate: updated},
        }
        concepts_by_id[subject_id] = refreshed
    return f"{subject_id}:{predicate}", updated != existing


def _bootstrap_canonical_concept_summary_fields(
    *,
    asset_path: str | Path | None = None,
    freshness_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialise canonical concept-summary field metadata into Vontology."""

    started_at = time.perf_counter()
    bundle = _load_seed_bundle(asset_path)
    source_tag = _text(bundle.get("source_tag")) or "JVNAUTOSCI-1958"
    managed_by = (
        _text(bundle.get("managed_by")) or "concept_summary_field_vontology_service"
    )
    errors: list[dict[str, str]] = []

    concept_specs = [
        spec for spec in bundle.get("concepts") or () if isinstance(spec, Mapping)
    ]
    text_specs = [
        spec for spec in bundle.get("text_relations") or () if isinstance(spec, Mapping)
    ]
    relationship_specs = [
        spec for spec in bundle.get("relationships") or () if isinstance(spec, Mapping)
    ]
    required_concept_ids = _strings(
        [
            *[_text(spec.get("concept_id")) for spec in concept_specs],
            *[_text(spec.get("subject_concept_id")) for spec in relationship_specs],
        ]
    )
    concepts_by_id: dict[str, Mapping[str, Any]] = dict(
        concept_service.get_concepts_by_concept_ids_exact(required_concept_ids)
    )
    text_query_metadata: dict[str, Any] = {}
    rows_by_concept = get_texts_for_concepts(
        [_text(spec.get("subject_concept_id")) for spec in text_specs],
        predicates=_strings([_text(spec.get("predicate")) for spec in text_specs]),
        limit_per_concept=50,
        query_metadata=text_query_metadata,
    )

    def apply_many(
        section: str,
        specs: Sequence[Mapping[str, Any]],
        callback,
    ) -> tuple[list[str], list[str]]:
        seen: list[str] = []
        changed: list[str] = []
        for spec in specs:
            identity = _text(
                spec.get("concept_id")
                or spec.get("subject_concept_id")
                or spec.get("predicate")
            )
            try:
                applied_id, did_change = callback(spec)
                seen.append(applied_id)
                if did_change:
                    changed.append(applied_id)
            except Exception as exc:
                errors.append(
                    {"section": section, "id": identity, "reason_code": str(exc)}
                )
        return seen, changed

    concept_ids, created_concept_ids = apply_many(
        "concepts",
        concept_specs,
        lambda spec: _ensure_concept(
            spec,
            source_tag=source_tag,
            managed_by=managed_by,
            concepts_by_id=concepts_by_id,
        ),
    )
    text_relation_ids, changed_text_relation_ids = apply_many(
        "text_relations",
        text_specs,
        lambda spec: _upsert_seed_text_relation(
            spec,
            source_tag=source_tag,
            managed_by=managed_by,
            current_rows=rows_by_concept.get(_text(spec.get("subject_concept_id")), []),
        ),
    )
    relationship_ids, changed_relationship_ids = apply_many(
        "relationships",
        relationship_specs,
        lambda spec: _merge_relationship(spec, concepts_by_id=concepts_by_id),
    )
    field_concept_ids = [
        concept_id
        for spec in concept_specs
        if SUMMARY_FIELD_TYPE_ID in _strings(spec.get("parent_concept_ids"))
        for concept_id in [_text(spec.get("concept_id"))]
        if concept_id in concept_ids
    ]

    changed = bool(
        created_concept_ids or changed_text_relation_ids or changed_relationship_ids
    )
    if changed:
        get_concept_summary_field_resolver().invalidate_cache()
    report: dict[str, Any] = {
        "success": not errors,
        "asset_path": bundle.get("asset_path"),
        "schema_version": bundle.get("schema_version"),
        "seed_version": bundle.get("seed_version"),
        "source_tag": source_tag,
        "managed_by": managed_by,
        "concept_ids": concept_ids,
        "created_concept_ids": created_concept_ids,
        "field_concept_ids": field_concept_ids,
        "text_relation_ids": text_relation_ids,
        "changed_text_relation_ids": changed_text_relation_ids,
        "relationship_ids": relationship_ids,
        "changed_relationship_ids": changed_relationship_ids,
        "changed": changed,
        "read_strategy": "batched_canonical_state",
        "read_phases": 2,
        "canonical_read_batches": {"concepts": 1, "text_assertions": 1},
        "text_query_metadata": text_query_metadata,
        "duration_ms": int((time.perf_counter() - started_at) * 1000),
        "errors": errors,
        "counts": {
            "concepts_seen": len(concept_ids),
            "concepts_created": len(created_concept_ids),
            "fields_seen": len(field_concept_ids),
            "field_configs_written": len(text_relation_ids),
            "field_configs_changed": len(changed_text_relation_ids),
            "type_bindings_written": len(relationship_ids),
            "type_bindings_changed": len(changed_relationship_ids),
            "errors": len(errors),
        },
    }
    if freshness_context is not None:
        canonical_read_complete = not bool(
            text_query_metadata.get("relation_query_truncated")
        )
        if report["success"] and not changed and canonical_read_complete:
            tracked_concept_document_ids = [
                concept.get("_id") or concept.get("id")
                for concept in concepts_by_id.values()
                if isinstance(concept, Mapping)
                and (concept.get("_id") is not None or concept.get("id") is not None)
            ]
            tracked_text_relation_document_ids = [
                row.get("relation_id")
                for rows in rows_by_concept.values()
                for row in rows
                if isinstance(row, Mapping) and row.get("relation_id") is not None
            ]
            tracked_text_value_document_ids = [
                row.get("text_value_id")
                for rows in rows_by_concept.values()
                for row in rows
                if isinstance(row, Mapping) and row.get("text_value_id") is not None
            ]
            receipt_result = seed_freshness.record_startup_seed_freshness(
                family_id=SUMMARY_FIELD_STARTUP_FAMILY_ID,
                source_digest=_text(freshness_context.get("source_digest")),
                producer_schema_version=(SUMMARY_FIELD_STARTUP_PRODUCER_SCHEMA_VERSION),
                concept_ids=required_concept_ids,
                text_relation_subject_ids=_strings(
                    [_text(spec.get("subject_concept_id")) for spec in text_specs]
                ),
                text_relation_predicates=_strings(
                    [_text(spec.get("predicate")) for spec in text_specs]
                ),
                tracked_concept_document_ids=tracked_concept_document_ids,
                tracked_text_relation_document_ids=(tracked_text_relation_document_ids),
                tracked_text_value_document_ids=tracked_text_value_document_ids,
                observation=(freshness_context.get("observation") or {}),
                metadata={
                    "schema_version": report.get("schema_version"),
                    "seed_version": report.get("seed_version"),
                    "source_tag": report.get("source_tag"),
                    "managed_by": report.get("managed_by"),
                    "counts": report.get("counts"),
                },
            )
        else:
            receipt_result = {
                "persisted": False,
                "reason": (
                    "canonical_verification_failed"
                    if not report["success"]
                    else (
                        "canonical_read_truncated"
                        if not canonical_read_complete
                        else "canonical_state_changed"
                    )
                ),
            }
        report["freshness_receipt"] = (
            seed_freshness.public_startup_seed_freshness_result(receipt_result)
        )
    return report


def bootstrap_canonical_concept_summary_fields(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Materialise canonical concept-summary metadata without cache shortcuts."""

    return _bootstrap_canonical_concept_summary_fields(asset_path=asset_path)


def ensure_concept_summary_fields_current_for_startup(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Use a complete dependency receipt before canonical startup verification."""

    started_at = time.perf_counter()
    bundle = _load_seed_bundle(asset_path)
    scope = _startup_freshness_scope(bundle)
    source_digest = _startup_source_digest(bundle)
    freshness_check = seed_freshness.check_startup_seed_freshness(
        family_id=SUMMARY_FIELD_STARTUP_FAMILY_ID,
        source_digest=source_digest,
        producer_schema_version=SUMMARY_FIELD_STARTUP_PRODUCER_SCHEMA_VERSION,
        concept_ids=scope["concept_ids"],
        text_relation_subject_ids=scope["text_relation_subject_ids"],
        text_relation_predicates=scope["text_relation_predicates"],
    )
    public_check = seed_freshness.public_startup_seed_freshness_result(freshness_check)
    if freshness_check.get("fresh"):
        metadata = freshness_check.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        return {
            "success": True,
            "skipped": True,
            "reason": "dependency_receipt_current",
            "changed": False,
            "asset_path": bundle.get("asset_path"),
            "schema_version": bundle.get("schema_version"),
            "seed_version": bundle.get("seed_version"),
            "source_tag": metadata.get("source_tag") or bundle.get("source_tag"),
            "managed_by": metadata.get("managed_by") or bundle.get("managed_by"),
            "read_strategy": "dependency_receipt",
            "read_phases": 1,
            "canonical_read_batches": {"concepts": 0, "text_assertions": 0},
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
            "freshness_receipt": public_check,
            "counts": dict(metadata.get("counts") or {}),
            "errors": [],
        }

    observation = seed_freshness.begin_startup_seed_freshness_observation()
    report = _bootstrap_canonical_concept_summary_fields(
        asset_path=asset_path,
        freshness_context={
            "source_digest": source_digest,
            "observation": observation,
        },
    )
    report["freshness_receipt_check"] = public_check
    report["duration_ms"] = int((time.perf_counter() - started_at) * 1000)
    return report


__all__ = [
    "SUMMARY_FIELD_SEED_SCHEMA_VERSION",
    "SUMMARY_FIELD_STARTUP_FAMILY_ID",
    "SUMMARY_FIELD_STARTUP_PRODUCER_SCHEMA_VERSION",
    "SUMMARY_FIELD_TYPE_ID",
    "bootstrap_canonical_concept_summary_fields",
    "ensure_concept_summary_fields_current_for_startup",
]
