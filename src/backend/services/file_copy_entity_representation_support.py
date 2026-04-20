"""Shared support helpers for file-copy entity representation materialisation."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import concept_search_service
from .text_value_service import upsert_text_for_concept
from .workflow_vontology_materialisation_helpers import stable_named_instance_concept_id


def safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def clean_string_list(values: Sequence[Any] | None) -> list[str]:
    seen: set[str] = set()
    cleaned_values: list[str] = []
    for raw in values or []:
        cleaned = safe_str(raw)
        if not cleaned:
            continue
        folded = cleaned.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        cleaned_values.append(cleaned)
    return cleaned_values


def write_text_relation(
    *,
    subject_concept_id: str,
    predicate: str,
    text: str,
    context: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        relation = upsert_text_for_concept(
            subject_concept_id=subject_concept_id,
            predicate=predicate,
            text=text,
            lang="en-NZ",
            context=dict(context or {}),
        )
        return dict(relation) if isinstance(relation, Mapping) else None, None
    except Exception as exc:
        return None, str(exc)


def resolve_or_create_named_instance_concept_id(
    *,
    user_concept_id: str,
    entity_name: str,
    search_instance_type_ids: Sequence[str],
    create_instance_type_id: str,
    prefix: str,
    system_tags: Sequence[str],
    logger: Any | None = None,
    logger_label: str,
) -> tuple[str, bool]:
    from . import concept_service

    for instance_type in search_instance_type_ids:
        search_result = concept_search_service.search_concepts(
            query=entity_name,
            instance_of=instance_type,
            match_type="exact",
            limit=10,
        )
        for item in search_result.get("results") or []:
            if not isinstance(item, Mapping):
                continue
            candidate_id = safe_str(item.get("concept_id"))
            candidate_name = safe_str(item.get("name"))
            if not candidate_id:
                continue
            if candidate_name and candidate_name.casefold() == entity_name.casefold():
                return candidate_id, False

    concept_id = stable_named_instance_concept_id(entity_name, prefix=prefix)
    try:
        concept_service.create_concept(
            name=entity_name,
            concept_id=concept_id,
            parent_concept_ids=[create_instance_type_id],
            create_as_instance=True,
            system_tags=list(system_tags),
        )
        concept_service.update_concept(
            concept_id,
            {"relationships.specific_to_user": [user_concept_id.strip()]},
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[%s] Concept create failed for %s: %s",
                logger_label,
                entity_name,
                exc,
            )
    return concept_id, True


__all__ = [
    "clean_string_list",
    "resolve_or_create_named_instance_concept_id",
    "safe_str",
    "write_text_relation",
]
