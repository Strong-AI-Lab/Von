"""Seed-only publication support for represented publication-scope profiles.

The repo JSON is a versioned release input, not request-time semantic
authority.  Startup creates missing schema/profile materialisation but preserves
an existing live profile payload so a Vontology edit changes subsequent
decisions without a Python release or being undone by restart.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import concept_service
from .publication_scope_profile_service import (
    PUBLICATION_SCOPE_PROFILE_LINK_PREDICATE,
    PUBLICATION_SCOPE_PROFILE_SCHEMA_VERSION,
    PUBLICATION_SCOPE_PROFILE_TEXT_PREDICATE,
)
from .text_value_service import (
    get_texts_for_concept,
    get_texts_for_concepts,
    upsert_singleton_text_relation,
)

PUBLICATION_SCOPE_PROFILE_SEED_SCHEMA_VERSION = (
    "publication_scope_profile_seed_bundle.v1"
)
PUBLICATION_SCOPE_PROFILE_TYPE_ID = "#V#publication_scope_profile"

_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "vontology"
    / "seed_bundles"
    / "publication_scope_profiles_seed_bundle.json"
)
_PROFILE_TEXT_PREDICATE_ALIASES = (
    PUBLICATION_SCOPE_PROFILE_TEXT_PREDICATE,
    "has_publication_scope_profile_json",
    "hasPublicationScopeProfileJson",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        token = _text(item)
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def _load_seed_bundle(asset_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(asset_path or _SEED_ASSET_PATH).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("publication_scope_profile_seed_bundle_not_mapping")
    if _text(payload.get("schema_version")) != (
        PUBLICATION_SCOPE_PROFILE_SEED_SCHEMA_VERSION
    ):
        raise ValueError("publication_scope_profile_seed_bundle_schema_unsupported")
    return {**dict(payload), "asset_path": str(path)}


def _get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = concept_service.get_concept_by_concept_id_exact(concept_id)
    except concept_service.ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _ensure_concept(
    spec: Mapping[str, Any],
    *,
    source_tag: str,
    managed_by: str,
    concepts_by_id: dict[str, Mapping[str, Any]],
) -> tuple[str, bool]:
    concept_id = _text(spec.get("concept_id"))
    name = _text(spec.get("name"))
    if not concept_id.startswith("#V#") or not name:
        raise ValueError("publication_scope_seed_concept_identity_invalid")
    if concept_id in concepts_by_id:
        return concept_id, False
    attributes = dict(spec.get("attributes") or {})
    attributes.update(
        {
            "repo_seed_role": "publication_scope_profile_release_input",
            "repo_seed_source_tag": source_tag,
            "repo_seed_managed_by": managed_by,
        }
    )
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
        maintain_relationship_inverses=False,
    )
    readback = _get_concept(concept_id)
    if readback is None:
        raise RuntimeError(
            f"publication_scope_seed_concept_readback_failed:{concept_id}"
        )
    concepts_by_id[concept_id] = dict(readback)
    return concept_id, True


def _merge_relationship(
    *,
    subject_concept_id: str,
    predicate: str,
    object_concept_ids: Sequence[str],
    concepts_by_id: dict[str, Mapping[str, Any]],
) -> tuple[str, bool]:
    subject_id = _text(subject_concept_id)
    predicate_id = _text(predicate)
    targets = _strings(object_concept_ids)
    if not subject_id.startswith("#V#") or not predicate_id or not targets:
        raise ValueError("publication_scope_seed_relationship_invalid")
    concept = concepts_by_id.get(subject_id)
    if not isinstance(concept, Mapping):
        raise TypeError(f"publication_scope_seed_subject_missing:{subject_id}")
    relationships = dict(concept.get("relationships") or {})
    existing = _strings(relationships.get(predicate_id))
    updated = _strings([*existing, *targets])
    changed = updated != existing
    if changed:
        concept_service.update_concept(
            subject_id,
            {f"relationships.{predicate_id}": updated},
        )
    readback = _get_concept(subject_id) if changed else concept
    readback_relationships = (
        readback.get("relationships") if isinstance(readback, Mapping) else None
    )
    readback_targets = _strings(
        readback_relationships.get(predicate_id)
        if isinstance(readback_relationships, Mapping)
        else None
    )
    if not set(targets).issubset(readback_targets):
        raise RuntimeError(
            f"publication_scope_seed_relationship_readback_failed:{subject_id}:{predicate_id}"
        )
    if isinstance(readback, Mapping):
        concepts_by_id[subject_id] = dict(readback)
    return f"{subject_id}:{predicate_id}", changed


def _profile_has_payload(profile_concept_id: str) -> bool:
    for predicate in _PROFILE_TEXT_PREDICATE_ALIASES:
        if get_texts_for_concept(
            profile_concept_id,
            predicate=predicate,
            limit=2,
        ):
            return True
    return False


def _profile_subject_ids(spec: Mapping[str, Any]) -> list[str]:
    return _strings(
        spec.get("bound_subject_concept_ids") or spec.get("bound_subject_concept_id")
    )


def bootstrap_canonical_publication_scope_profiles(
    *,
    asset_path: str | Path | None = None,
    force_profile_seed: bool = False,
) -> dict[str, Any]:
    """Materialise missing represented profiles and read them back exactly."""

    started_at = time.perf_counter()
    bundle = _load_seed_bundle(asset_path)
    source_tag = _text(bundle.get("source_tag")) or "JVNAUTOSCI-2671"
    managed_by = (
        _text(bundle.get("managed_by")) or "publication_scope_profile_vontology_service"
    )
    errors: list[dict[str, str]] = []
    created_concept_ids: list[str] = []
    existing_concept_ids: list[str] = []
    seeded_profile_ids: list[str] = []
    preserved_profile_ids: list[str] = []
    relationship_ids: list[str] = []
    changed_relationship_ids: list[str] = []

    concept_specs: list[Mapping[str, Any]] = [
        spec for spec in bundle.get("concepts") or () if isinstance(spec, Mapping)
    ]
    profile_specs: list[Mapping[str, Any]] = [
        spec for spec in bundle.get("profiles") or () if isinstance(spec, Mapping)
    ]
    for profile_spec in profile_specs:
        concept_specs.append(
            {
                "concept_id": profile_spec.get("profile_concept_id"),
                "name": profile_spec.get("name"),
                "description": (
                    (profile_spec.get("payload") or {}).get("description")
                    if isinstance(profile_spec.get("payload"), Mapping)
                    else None
                ),
                "parent_concept_ids": [PUBLICATION_SCOPE_PROFILE_TYPE_ID],
                "create_as_instance": True,
                "system_tags": ["publication-scope-profile", "semantic-advice"],
            }
        )

    relationship_specs: list[Mapping[str, Any]] = [
        spec for spec in bundle.get("relationships") or () if isinstance(spec, Mapping)
    ]
    for profile_spec in profile_specs:
        profile_id = _text(profile_spec.get("profile_concept_id"))
        for subject_id in _profile_subject_ids(profile_spec):
            relationship_specs.append(
                {
                    "subject_concept_id": subject_id,
                    "predicate": PUBLICATION_SCOPE_PROFILE_LINK_PREDICATE,
                    "object_concept_ids": [profile_id],
                }
            )

    required_concept_ids = _strings(
        [
            *[_text(spec.get("concept_id")) for spec in concept_specs],
            *[_text(spec.get("subject_concept_id")) for spec in relationship_specs],
        ]
    )
    concepts_by_id: dict[str, Mapping[str, Any]] = dict(
        concept_service.get_concepts_by_concept_ids_exact(required_concept_ids)
    )
    profile_ids = [_text(spec.get("profile_concept_id")) for spec in profile_specs]
    profile_query_metadata: dict[str, Any] = {}
    profile_rows_by_id = get_texts_for_concepts(
        profile_ids,
        predicates=_PROFILE_TEXT_PREDICATE_ALIASES,
        limit_per_concept=10,
        query_metadata=profile_query_metadata,
    )
    profile_ids_with_payload = {
        profile_id
        for profile_id, rows in profile_rows_by_id.items()
        if any(_text(row.get("text")) for row in rows)
    }

    for spec in concept_specs:
        concept_id = _text(spec.get("concept_id"))
        try:
            resolved_id, created = _ensure_concept(
                spec,
                source_tag=source_tag,
                managed_by=managed_by,
                concepts_by_id=concepts_by_id,
            )
            (created_concept_ids if created else existing_concept_ids).append(
                resolved_id
            )
        except (
            TypeError,
            ValueError,
            RuntimeError,
            concept_service.ConceptServiceError,
        ) as exc:
            errors.append(
                {
                    "stage": "concept",
                    "concept_id": concept_id,
                    "reason_code": str(exc),
                }
            )

    for spec in relationship_specs:
        subject_id = _text(spec.get("subject_concept_id"))
        predicate = _text(spec.get("predicate"))
        try:
            relationship_id, changed = _merge_relationship(
                subject_concept_id=subject_id,
                predicate=predicate,
                object_concept_ids=_strings(spec.get("object_concept_ids")),
                concepts_by_id=concepts_by_id,
            )
            relationship_ids.append(relationship_id)
            if changed:
                changed_relationship_ids.append(relationship_id)
        except (
            TypeError,
            ValueError,
            RuntimeError,
            concept_service.ConceptServiceError,
        ) as exc:
            errors.append(
                {
                    "stage": "relationship",
                    "concept_id": subject_id,
                    "reason_code": str(exc),
                }
            )

    for spec in profile_specs:
        profile_id = _text(spec.get("profile_concept_id"))
        payload = spec.get("payload")
        if not profile_id or not isinstance(payload, Mapping):
            errors.append(
                {
                    "stage": "profile",
                    "concept_id": profile_id,
                    "reason_code": "publication_scope_seed_profile_invalid",
                }
            )
            continue
        if _text(payload.get("schema_version")) != (
            PUBLICATION_SCOPE_PROFILE_SCHEMA_VERSION
        ):
            errors.append(
                {
                    "stage": "profile",
                    "concept_id": profile_id,
                    "reason_code": "publication_scope_seed_profile_schema_invalid",
                }
            )
            continue
        try:
            if force_profile_seed or profile_id not in profile_ids_with_payload:
                upsert_singleton_text_relation(
                    subject_concept_id=profile_id,
                    predicate=PUBLICATION_SCOPE_PROFILE_TEXT_PREDICATE,
                    text=json.dumps(payload, ensure_ascii=True, sort_keys=True),
                    lang="en-NZ",
                    context={
                        "schema_version": PUBLICATION_SCOPE_PROFILE_SEED_SCHEMA_VERSION,
                        "seed_version": _text(bundle.get("seed_version")),
                        "source": source_tag,
                        "managed_by": managed_by,
                        "repo_seed_role": "release_input",
                    },
                    garbage_collect=True,
                )
                if not _profile_has_payload(profile_id):
                    raise RuntimeError("publication_scope_profile_text_readback_failed")
                profile_ids_with_payload.add(profile_id)
                seeded_profile_ids.append(profile_id)
            else:
                preserved_profile_ids.append(profile_id)
        except (
            TypeError,
            ValueError,
            RuntimeError,
            concept_service.ConceptServiceError,
        ) as exc:
            errors.append(
                {
                    "stage": "profile",
                    "concept_id": profile_id,
                    "reason_code": str(exc),
                }
            )

    return {
        "success": not errors,
        "schema_version": bundle.get("schema_version"),
        "seed_version": bundle.get("seed_version"),
        "asset_path": bundle.get("asset_path"),
        "source_tag": source_tag,
        "managed_by": managed_by,
        "created_concept_ids": created_concept_ids,
        "existing_concept_ids": existing_concept_ids,
        "seeded_profile_ids": seeded_profile_ids,
        "preserved_profile_ids": preserved_profile_ids,
        "relationship_ids": relationship_ids,
        "changed_relationship_ids": changed_relationship_ids,
        "changed": bool(
            created_concept_ids or seeded_profile_ids or changed_relationship_ids
        ),
        "read_strategy": "batched_canonical_state",
        "read_phases": 2,
        "canonical_read_batches": {"concepts": 1, "text_assertions": 1},
        "profile_query_metadata": profile_query_metadata,
        "duration_ms": int((time.perf_counter() - started_at) * 1000),
        "errors": errors,
        "counts": {
            "concepts_created": len(created_concept_ids),
            "concepts_existing": len(existing_concept_ids),
            "profiles_seeded": len(seeded_profile_ids),
            "profiles_preserved": len(preserved_profile_ids),
            "relationships_seen": len(relationship_ids),
            "relationships_changed": len(changed_relationship_ids),
            "errors": len(errors),
        },
    }


__all__ = [
    "PUBLICATION_SCOPE_PROFILE_SEED_SCHEMA_VERSION",
    "PUBLICATION_SCOPE_PROFILE_TYPE_ID",
    "bootstrap_canonical_publication_scope_profiles",
]
