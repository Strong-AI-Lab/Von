"""Durable represented markers for source-item processing.

This service is a support surface for VWL workflows.  It records and reads
source-processing evidence as Vontology artefacts, while workflows decide when
that evidence should be written or consulted.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from . import concept_service
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from .workflow_vontology_materialisation_helpers import (
    suspend_event_workflow_integration,
)

SOURCE_PROCESSING_MARKER_TYPE_ID = "#V#source_processing_marker"
SOURCE_PROCESSING_EVIDENCE_PREDICATE_ID = "#V#hasSourceProcessingEvidenceJson"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ARXIV_ID_RE = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b")

_SUPPORT_TYPE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "concept_id": "#V#abstract_object",
        "name": "Abstract object",
        "parent_concept_ids": ["#V#thing"],
        "create_as_instance": False,
        "description": "A non-physical object used as a parent for represented specifications and markers.",
    },
    {
        "concept_id": "#V#information_object",
        "name": "Information object",
        "parent_concept_ids": ["#V#abstract_object"],
        "create_as_instance": False,
        "description": "An abstract object whose primary function is carrying or specifying information.",
    },
    {
        "concept_id": "#V#predicate",
        "name": "Predicate",
        "parent_concept_ids": ["#V#abstract_object"],
        "create_as_instance": False,
        "description": "A Vontology concept used as a relationship or text-relation predicate.",
    },
    {
        "concept_id": "#V#workflow_coordination_artefact",
        "name": "Workflow Coordination Artefact",
        "parent_concept_ids": ["#V#information_object"],
        "create_as_instance": False,
        "description": "A represented artefact used to coordinate workflow state, reuse, or decisions.",
    },
    {
        "concept_id": "#V#workflow_marker",
        "name": "Workflow Marker",
        "parent_concept_ids": ["#V#workflow_coordination_artefact"],
        "create_as_instance": False,
        "description": "A workflow coordination artefact that marks a durable state, checkpoint, skip condition, or completion condition.",
    },
    {
        "concept_id": SOURCE_PROCESSING_MARKER_TYPE_ID,
        "name": "Source Processing Marker",
        "parent_concept_ids": ["#V#workflow_marker"],
        "create_as_instance": False,
        "description": "A workflow marker recording that a source-system item has been processed into represented artefacts.",
    },
    {
        "concept_id": SOURCE_PROCESSING_EVIDENCE_PREDICATE_ID,
        "name": "hasSourceProcessingEvidenceJson",
        "parent_concept_ids": ["#V#predicate"],
        "create_as_instance": True,
        "description": "Text-relation predicate for a JSON source-processing evidence packet attached to a workflow marker.",
    },
)

_CONCEPT_ID_KEYS = {
    "paper_concept_id",
    "represented_artifact_concept_id",
    "represented_artefact_concept_id",
    "artefact_concept_id",
    "artifact_concept_id",
}
_CONCEPT_ID_LIST_KEYS = {
    "paper_concept_ids",
    "represented_artifact_concept_ids",
    "represented_artefact_concept_ids",
    "artefact_concept_ids",
    "artifact_concept_ids",
}
_FILE_COPY_KEYS = {"file_copy_concept_id"}
_FILE_COPY_LIST_KEYS = {"file_copy_concept_ids"}


def _clean_text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _ordered_unique(values: Sequence[Any]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for value in values:
        text = _clean_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        results.append(text)
    return results


def _as_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return [value]


def _walk_json(value: Any) -> list[Any]:
    items: list[Any] = [value]
    if isinstance(value, Mapping):
        for child in value.values():
            items.extend(_walk_json(child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            items.extend(_walk_json(child))
    return items


def _iter_scalar_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_clean_text(value)] if _clean_text(value) else []
    if isinstance(value, (int, float, bool)):
        return [_clean_text(value)]
    if isinstance(value, Mapping):
        texts: list[str] = []
        for child in value.values():
            texts.extend(_iter_scalar_texts(child))
        return texts
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        texts = []
        for child in value:
            texts.extend(_iter_scalar_texts(child))
        return texts
    return []


def _concept_exists(concept_id: str) -> bool:
    try:
        return concept_service.get_concept_by_concept_id_exact(concept_id) is not None
    except concept_service.ConceptNotFoundError:
        return False


def _ensure_support_concepts() -> list[str]:
    created: list[str] = []
    with suspend_event_workflow_integration():
        for spec in _SUPPORT_TYPE_SPECS:
            concept_id = str(spec["concept_id"])
            if _concept_exists(concept_id):
                continue
            concept_service.create_concept(
                name=str(spec["name"]),
                concept_id=concept_id,
                parent_concept_ids=list(spec["parent_concept_ids"]),
                create_as_instance=bool(spec["create_as_instance"]),
                description=str(spec["description"]),
                visibility_scope_mode="global_general",
                system_tags=["source-processing-marker", "workflow-support"],
            )
            created.append(concept_id)
    return created


def source_processing_marker_concept_id(
    *,
    source_system: str,
    source_item_id: str,
    source_profile: str | None = None,
) -> str:
    """Return the deterministic marker concept id for a source item."""

    source_system_clean = _clean_text(source_system).lower()
    source_item_clean = _clean_text(source_item_id)
    source_profile_clean = _clean_text(source_profile).lower()
    basis = json.dumps(
        [source_system_clean, source_profile_clean, source_item_clean],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    slug_source = "_".join(
        item
        for item in (source_system_clean, source_profile_clean, source_item_clean)
        if item
    )
    slug = _SLUG_RE.sub("_", slug_source.lower()).strip("_") or "source_item"
    if len(slug) > 96:
        slug = slug[:96].rstrip("_")
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]
    return f"#V#source_processing_marker_{slug}_{digest}"


def _extract_concept_ids_from_outputs(value: Any) -> tuple[list[str], list[str]]:
    represented_ids: list[str] = []
    file_copy_ids: list[str] = []
    for candidate in _walk_json(value):
        if not isinstance(candidate, Mapping):
            continue
        for raw_key, raw_value in candidate.items():
            key = _clean_text(raw_key).lower()
            if key in _CONCEPT_ID_KEYS or (
                "concept_id" in key
                and any(marker in key for marker in ("paper", "artefact", "artifact"))
            ):
                represented_ids.extend(_iter_scalar_texts(raw_value))
            elif key in _CONCEPT_ID_LIST_KEYS:
                represented_ids.extend(_iter_scalar_texts(raw_value))
            elif key in _FILE_COPY_KEYS or key in _FILE_COPY_LIST_KEYS:
                file_copy_ids.extend(_iter_scalar_texts(raw_value))
    return _ordered_unique(represented_ids), _ordered_unique(file_copy_ids)


def _extract_arxiv_ids(value: Any) -> list[str]:
    arxiv_ids: list[str] = []
    for candidate in _walk_json(value):
        if not isinstance(candidate, Mapping):
            continue
        for raw_key, raw_value in candidate.items():
            key = _clean_text(raw_key).lower()
            if "arxiv" not in key:
                continue
            for text in _iter_scalar_texts(raw_value):
                for match in _ARXIV_ID_RE.finditer(text):
                    arxiv_ids.append(match.group(0))
    return _ordered_unique(arxiv_ids)


def _first_or_none(values: Sequence[str]) -> str | None:
    return values[0] if values else None


def _marker_response_from_payload(
    *,
    marker_concept_id: str,
    source_item_id: str,
    marker_exists: bool,
    marker_created: bool = False,
    evidence_payload: Mapping[str, Any] | None = None,
    support_concepts_created: Sequence[str] = (),
    text_relation_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(evidence_payload or {})
    represented_ids = _ordered_unique(
        _as_sequence(payload.get("represented_artifact_concept_ids"))
        + _as_sequence(payload.get("represented_artefact_concept_ids"))
        + _as_sequence(payload.get("paper_concept_ids"))
    )
    file_copy_ids = _ordered_unique(_as_sequence(payload.get("file_copy_concept_ids")))
    arxiv_ids = _ordered_unique(_as_sequence(payload.get("arxiv_ids")))
    response: dict[str, Any] = {
        "success": True,
        "schema_version": "source_processing_marker.result.v1",
        "source_processing_marker": marker_concept_id,
        "source_processing_marker_exists": marker_exists,
        "source_processing_marker_created": marker_created,
        "marker_concept_id": marker_concept_id,
        "message_processing_marker": marker_concept_id if marker_exists else None,
        "message_processing_marker_seen": marker_exists,
        "message_processing_status": "processed" if marker_exists else "unprocessed",
        "message_processed": marker_exists,
        "source_message_processed": marker_exists,
        "processed_source_item_id": source_item_id if marker_exists else None,
        "processed_message_id": source_item_id if marker_exists else None,
        "represented_artifact_concept_ids": represented_ids,
        "represented_artefact_concept_ids": represented_ids,
        "paper_concept_ids": represented_ids,
        "paper_concept_id": _first_or_none(represented_ids),
        "file_copy_concept_ids": file_copy_ids,
        "file_copy_concept_id": _first_or_none(file_copy_ids),
        "arxiv_ids": arxiv_ids,
        "arxiv_id": _first_or_none(arxiv_ids),
        "support_concepts_created": list(support_concepts_created),
    }
    if text_relation_result is not None:
        response["source_processing_evidence_text_relation"] = dict(
            text_relation_result
        )
    if payload:
        response["source_processing_evidence"] = payload
    return response


def _load_marker_payload(marker_concept_id: str) -> dict[str, Any] | None:
    rows = get_texts_for_concept(
        marker_concept_id,
        predicate=SOURCE_PROCESSING_EVIDENCE_PREDICATE_ID,
        limit=5,
    )
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        text = _clean_text(row.get("text"))
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def get_source_processing_marker(
    *,
    source_system: str,
    source_item_id: str,
    source_profile: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    source_system_clean = _clean_text(source_system)
    source_item_clean = _clean_text(source_item_id)
    if not source_system_clean or not source_item_clean:
        return {
            "success": False,
            "error_code": "missing_source_processing_marker_key",
            "error": "source_system and source_item_id are required.",
        }
    marker_concept_id = source_processing_marker_concept_id(
        source_system=source_system_clean,
        source_profile=source_profile,
        source_item_id=source_item_clean,
    )
    marker_exists = _concept_exists(marker_concept_id)
    payload = _load_marker_payload(marker_concept_id) if marker_exists else None
    return _marker_response_from_payload(
        marker_concept_id=marker_concept_id,
        source_item_id=source_item_clean,
        marker_exists=marker_exists,
        evidence_payload=payload,
    )


def record_source_processing_marker(
    *,
    source_system: str,
    source_item_id: str,
    source_profile: str | None = None,
    workflow_id: str | None = None,
    processing_status: str | None = None,
    represented_artifact_concept_ids: Sequence[Any] | None = None,
    represented_artefact_concept_ids: Sequence[Any] | None = None,
    paper_concept_ids: Sequence[Any] | None = None,
    file_copy_concept_ids: Sequence[Any] | None = None,
    arxiv_ids: Sequence[Any] | None = None,
    represented_outputs: Any = None,
    namespace: str | None = None,
    created_by_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    source_system_clean = _clean_text(source_system)
    source_item_clean = _clean_text(source_item_id)
    source_profile_clean = _clean_text(source_profile)
    if not source_system_clean or not source_item_clean:
        return {
            "success": False,
            "error_code": "missing_source_processing_marker_key",
            "error": "source_system and source_item_id are required.",
        }

    support_created = _ensure_support_concepts()
    marker_concept_id = source_processing_marker_concept_id(
        source_system=source_system_clean,
        source_profile=source_profile_clean,
        source_item_id=source_item_clean,
    )
    marker_exists = _concept_exists(marker_concept_id)
    marker_created = False
    if not marker_exists:
        with suspend_event_workflow_integration():
            concept_service.create_concept(
                name=(
                    f"Source processing marker: {source_system_clean}"
                    f" {source_profile_clean} {source_item_clean}"
                ).strip(),
                concept_id=marker_concept_id,
                parent_concept_ids=[SOURCE_PROCESSING_MARKER_TYPE_ID],
                create_as_instance=True,
                description=(
                    "Durable marker that a source-system item has been processed "
                    "into represented artefacts by a workflow."
                ),
                created_by_concept_id=created_by_concept_id,
                organisation_concept_id=organisation_concept_id,
                event_namespace=namespace,
                visibility_scope_mode="user_org_default",
                system_tags=["source-processing-marker", "workflow-support"],
            )
        marker_created = True

    extracted_ids, extracted_file_copy_ids = _extract_concept_ids_from_outputs(
        represented_outputs
    )
    extracted_arxiv_ids = _extract_arxiv_ids(represented_outputs)
    represented_ids = _ordered_unique(
        _as_sequence(represented_artifact_concept_ids)
        + _as_sequence(represented_artefact_concept_ids)
        + _as_sequence(paper_concept_ids)
        + extracted_ids
    )
    file_copy_ids = _ordered_unique(
        _as_sequence(file_copy_concept_ids) + extracted_file_copy_ids
    )
    arxiv_id_values = _ordered_unique(_as_sequence(arxiv_ids) + extracted_arxiv_ids)
    status = _clean_text(processing_status) or "processed"
    evidence_payload: dict[str, Any] = {
        "schema_version": "source_processing_marker.v1",
        "source_system": source_system_clean,
        "source_profile": source_profile_clean or None,
        "source_item_id": source_item_clean,
        "processed_source_item_id": source_item_clean,
        "processed_message_id": source_item_clean,
        "workflow_id": _clean_text(workflow_id) or None,
        "processing_status": status,
        "message_processing_status": status,
        "message_processing_marker": marker_concept_id,
        "source_processing_marker": marker_concept_id,
        "represented_artifact_concept_ids": represented_ids,
        "represented_artefact_concept_ids": represented_ids,
        "paper_concept_ids": represented_ids,
        "paper_concept_id": _first_or_none(represented_ids),
        "file_copy_concept_ids": file_copy_ids,
        "file_copy_concept_id": _first_or_none(file_copy_ids),
        "arxiv_ids": arxiv_id_values,
        "arxiv_id": _first_or_none(arxiv_id_values),
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    text_relation = upsert_singleton_text_relation(
        subject_concept_id=marker_concept_id,
        predicate=SOURCE_PROCESSING_EVIDENCE_PREDICATE_ID,
        text=json.dumps(evidence_payload, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={"source": "source_processing_marker_service"},
        garbage_collect=True,
    )
    return _marker_response_from_payload(
        marker_concept_id=marker_concept_id,
        source_item_id=source_item_clean,
        marker_exists=True,
        marker_created=marker_created,
        evidence_payload=evidence_payload,
        support_concepts_created=support_created,
        text_relation_result=text_relation,
    )


__all__ = [
    "SOURCE_PROCESSING_EVIDENCE_PREDICATE_ID",
    "SOURCE_PROCESSING_MARKER_TYPE_ID",
    "get_source_processing_marker",
    "record_source_processing_marker",
    "source_processing_marker_concept_id",
]
