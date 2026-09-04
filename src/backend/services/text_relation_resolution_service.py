"""Deterministic concept resolution from an exact text relation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import filter_accessible_concept_ids
from ..vontology.code_concepts_registry import is_code_concept_id
from ..vontology.utils_vontology import get_vontology_node_and_descendant_ids
from .text_relation_read_policy import is_hidden_from_generic_text_reads

_MAX_RESULTS = 20
_DEFAULT_MAX_RESULTS = 5
_MAX_EXACT_TEXT_VALUES = 200
_MAX_EXACT_TEXT_RELATIONS = 2_000


def _rows(cursor: Iterable[Any]) -> list[Mapping[str, Any]]:
    return [row for row in cursor if isinstance(row, Mapping)]


def _result(
    *,
    predicate: str,
    text: str,
    instance_of: str | None,
    status: str,
    resolved_concept_id: str | None,
    candidate_ids: list[str],
    max_results: int,
    resolution_complete: bool,
    incomplete_stages: list[str] | None = None,
    success: bool = True,
    error_code: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    candidate_count = 1 if resolved_concept_id is not None else len(candidate_ids)
    candidates_truncated = len(candidate_ids) > max_results
    payload: dict[str, Any] = {
        "success": success,
        "status": status,
        "predicate": predicate,
        "text": text,
        "instance_of": instance_of,
        "resolved_concept_id": resolved_concept_id,
        "candidates": [
            {"concept_id": concept_id} for concept_id in candidate_ids[:max_results]
        ],
        "candidate_count": candidate_count,
        "candidate_count_is_lower_bound": not resolution_complete,
        "candidates_truncated": candidates_truncated,
        "resolution_complete": resolution_complete,
    }
    if incomplete_stages:
        payload["incomplete_stages"] = incomplete_stages
    if error_code:
        payload["error_code"] = error_code
    if error:
        payload["error"] = error
    return payload


def _normalise_instance_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str) and item}
    return set()


def _document_instance_ids(document: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(document, Mapping):
        return set()
    relationships = document.get("relationships")
    if not isinstance(relationships, Mapping):
        return set()
    return _normalise_instance_ids(
        relationships.get("is_an_instance_of")
    ) | _normalise_instance_ids(relationships.get("#V#is_an_instance_of"))


def resolve_concept_by_text_relation(
    *,
    predicate: str,
    text: str,
    instance_of: str | None = None,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """Resolve an actor-visible concept carrying an exact predicate/text pair.

    The active access-control context is authoritative. The function never
    chooses between multiple matches, and a bounded-but-incomplete scan cannot
    produce a resolved or not-found claim.
    """

    exact_predicate = predicate.strip() if isinstance(predicate, str) else ""
    exact_text = text.strip() if isinstance(text, str) else ""
    type_filter = (
        instance_of.strip()
        if isinstance(instance_of, str) and instance_of.strip()
        else None
    )
    valid_max_results = (
        isinstance(max_results, int)
        and not isinstance(max_results, bool)
        and 1 <= max_results <= _MAX_RESULTS
    )
    if not exact_predicate or not exact_text or not valid_max_results:
        invalid_fields: list[str] = []
        if not exact_predicate:
            invalid_fields.append("predicate")
        if not exact_text:
            invalid_fields.append("text")
        if not valid_max_results:
            invalid_fields.append("max_results")
        return _result(
            predicate=exact_predicate,
            text=exact_text,
            instance_of=type_filter,
            status="not_found",
            resolved_concept_id=None,
            candidate_ids=[],
            max_results=(
                max_results
                if isinstance(max_results, int)
                and not isinstance(max_results, bool)
                and max_results > 0
                else _DEFAULT_MAX_RESULTS
            ),
            resolution_complete=False,
            success=False,
            error_code="invalid_parameter",
            error=(
                "predicate and text must be non-empty strings, and max_results "
                f"must be an integer from 1 to {_MAX_RESULTS}. Invalid: "
                f"{', '.join(invalid_fields)}"
            ),
        )

    if is_hidden_from_generic_text_reads(exact_predicate):
        return _result(
            predicate=exact_predicate,
            text=exact_text,
            instance_of=type_filter,
            status="not_found",
            resolved_concept_id=None,
            candidate_ids=[],
            max_results=max_results,
            resolution_complete=True,
        )

    text_rows_with_sentinel = _rows(
        TextValuesRepository.find(
            {"text": exact_text},
            projection={"_id": 1},
            limit=_MAX_EXACT_TEXT_VALUES + 1,
        )
    )
    text_scan_saturated = len(text_rows_with_sentinel) > _MAX_EXACT_TEXT_VALUES
    text_rows = text_rows_with_sentinel[:_MAX_EXACT_TEXT_VALUES]

    text_value_ids: list[Any] = []
    for row in text_rows:
        raw_id = row.get("_id")
        if raw_id is None:
            continue
        text_value_ids.append(raw_id)
        raw_id_text = str(raw_id)
        if raw_id_text != raw_id:
            text_value_ids.append(raw_id_text)

    relation_rows_with_sentinel: list[Mapping[str, Any]] = []
    if text_value_ids:
        relation_rows_with_sentinel = _rows(
            TextRelationsRepository.find(
                {
                    "object_text_id": {"$in": text_value_ids},
                    "predicate": exact_predicate,
                },
                projection={"subject_concept_id": 1},
                limit=_MAX_EXACT_TEXT_RELATIONS + 1,
            )
        )
    relation_scan_saturated = (
        len(relation_rows_with_sentinel) > _MAX_EXACT_TEXT_RELATIONS
    )
    relation_rows = relation_rows_with_sentinel[:_MAX_EXACT_TEXT_RELATIONS]
    incomplete_stages = [
        stage
        for stage, saturated in (
            ("text_values", text_scan_saturated),
            ("text_relations", relation_scan_saturated),
        )
        if saturated
    ]

    raw_candidate_ids = {
        candidate.strip()
        for row in relation_rows
        if isinstance((candidate := row.get("subject_concept_id")), str)
        and candidate.strip()
    }
    accessible_ids = filter_accessible_concept_ids(raw_candidate_ids)

    documents = list(
        ConceptsRepository.find(
            {"concept_id": {"$in": sorted(accessible_ids)}},
            {"concept_id": 1, "relationships": 1},
        )
    )
    documents_by_id = {
        concept_id: row
        for row in documents
        if isinstance(row, Mapping)
        and isinstance((concept_id := row.get("concept_id")), str)
        and concept_id
    }
    existing_ids = set(documents_by_id)
    existing_ids.update(
        concept_id for concept_id in accessible_ids if is_code_concept_id(concept_id)
    )

    if type_filter and existing_ids:
        try:
            descendant_ids = get_vontology_node_and_descendant_ids(type_filter)
        except Exception as exc:  # noqa: BLE001 - preserve typed resolver failure
            return _result(
                predicate=exact_predicate,
                text=exact_text,
                instance_of=type_filter,
                status="not_found",
                resolved_concept_id=None,
                candidate_ids=[],
                max_results=max_results,
                resolution_complete=False,
                success=False,
                error_code="instance_of_resolution_failed",
                error=(
                    "Could not expand the instance_of restriction: "
                    f"{type(exc).__name__}"
                ),
            )
        accepted_types = {
            str(concept_id)
            for concept_id in (descendant_ids or [type_filter])
            if concept_id
        }
        existing_ids = {
            concept_id
            for concept_id in existing_ids
            if bool(
                accepted_types & _document_instance_ids(documents_by_id.get(concept_id))
            )
        }

    candidate_ids = sorted(existing_ids)
    resolution_complete = not incomplete_stages
    if not resolution_complete:
        return _result(
            predicate=exact_predicate,
            text=exact_text,
            instance_of=type_filter,
            status="ambiguous",
            resolved_concept_id=None,
            candidate_ids=candidate_ids,
            max_results=max_results,
            resolution_complete=False,
            incomplete_stages=incomplete_stages,
        )
    if not candidate_ids:
        return _result(
            predicate=exact_predicate,
            text=exact_text,
            instance_of=type_filter,
            status="not_found",
            resolved_concept_id=None,
            candidate_ids=[],
            max_results=max_results,
            resolution_complete=True,
        )
    if len(candidate_ids) == 1:
        return _result(
            predicate=exact_predicate,
            text=exact_text,
            instance_of=type_filter,
            status="resolved",
            resolved_concept_id=candidate_ids[0],
            candidate_ids=[],
            max_results=max_results,
            resolution_complete=True,
        )
    return _result(
        predicate=exact_predicate,
        text=exact_text,
        instance_of=type_filter,
        status="ambiguous",
        resolved_concept_id=None,
        candidate_ids=candidate_ids,
        max_results=max_results,
        resolution_complete=True,
    )
