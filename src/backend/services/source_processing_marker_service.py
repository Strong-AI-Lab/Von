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
SOURCE_PROCESSING_MARKER_EXISTENCE_LOOKUP_MAX_TIME_MS = 3_000

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
    "kr_concept_id",
    "kr_readback_concept_id",
}
_CONCEPT_ID_LIST_KEYS = {
    "paper_concept_ids",
    "represented_artifact_concept_ids",
    "represented_artefact_concept_ids",
    "artefact_concept_ids",
    "artifact_concept_ids",
    "kr_concept_ids",
    "kr_readback_concept_ids",
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


def _find_existing_concept_ids(concept_ids: Sequence[str]) -> set[str]:
    from ..db.repositories.concepts_repository import ConceptsRepository
    from ..utils.concept_id_utils import canonicalise_vontology_concept_id

    requested_ids = _ordered_unique(list(concept_ids))
    if not requested_ids:
        return set()
    lookup_ids = _ordered_unique(
        [
            candidate
            for concept_id in requested_ids
            for candidate in (
                concept_id,
                canonicalise_vontology_concept_id(concept_id),
            )
        ]
    )
    stored_ids = {
        _clean_text(row.get("concept_id"))
        for row in ConceptsRepository.find(
            {"concept_id": {"$in": lookup_ids}},
            {"_id": 0, "concept_id": 1},
            limit=len(lookup_ids),
            max_time_ms=SOURCE_PROCESSING_MARKER_EXISTENCE_LOOKUP_MAX_TIME_MS,
        )
        if isinstance(row, Mapping) and _clean_text(row.get("concept_id"))
    }
    return {
        concept_id
        for concept_id in requested_ids
        if concept_id in stored_ids
        or canonicalise_vontology_concept_id(concept_id) in stored_ids
    }


def _ensure_support_concepts(*, existing_ids: set[str] | None = None) -> list[str]:
    # These are global workflow-support concepts.  Resolve their exact IDs in
    # one bounded read instead of seven serial Atlas round trips on every
    # source-marker write; the latter can consume the entire MCP write deadline
    # before the marker evidence itself is persisted.
    support_ids = [str(spec["concept_id"]) for spec in _SUPPORT_TYPE_SPECS]
    known_existing_ids = (
        set(existing_ids)
        if existing_ids is not None
        else _find_existing_concept_ids(support_ids)
    )

    created: list[str] = []
    with suspend_event_workflow_integration():
        for spec in _SUPPORT_TYPE_SPECS:
            concept_id = str(spec["concept_id"])
            if concept_id in known_existing_ids:
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
    expected_source_fingerprint: str | None = None,
    expected_processing_authority_fingerprint: str | None = None,
) -> dict[str, Any]:
    payload = dict(evidence_payload or {})
    represented_ids = _ordered_unique(
        _as_sequence(payload.get("represented_artifact_concept_ids"))
        + _as_sequence(payload.get("represented_artefact_concept_ids"))
        + _as_sequence(payload.get("paper_concept_ids"))
    )
    file_copy_ids = _ordered_unique(_as_sequence(payload.get("file_copy_concept_ids")))
    arxiv_ids = _ordered_unique(_as_sequence(payload.get("arxiv_ids")))
    stored_source_fingerprint = _clean_text(payload.get("source_fingerprint"))
    stored_processing_status = _clean_text(payload.get("processing_status")).lower()
    expected_fingerprint = _clean_text(expected_source_fingerprint)
    stored_authority_fingerprint = _clean_text(
        payload.get("processing_authority_fingerprint")
    )
    expected_authority_fingerprint = _clean_text(
        expected_processing_authority_fingerprint
    )
    source_fingerprint_matches = bool(
        marker_exists
        and expected_fingerprint
        and stored_source_fingerprint == expected_fingerprint
    )
    source_system = _clean_text(payload.get("source_system")).lower()
    represented_outputs_required = source_system in {
        "spreadsheet_dataset",
        "spreadsheet_record",
    }
    represented_ids_exist = True
    if represented_outputs_required and represented_ids:
        represented_ids_exist = set(represented_ids).issubset(
            _find_existing_concept_ids(represented_ids)
        )
    processing_authority_matches = bool(
        not expected_authority_fingerprint
        or stored_authority_fingerprint == expected_authority_fingerprint
    )
    source_processing_current = bool(
        source_fingerprint_matches
        and stored_processing_status in {"complete", "completed", "processed"}
        and (not represented_outputs_required or bool(represented_ids))
        and represented_ids_exist
        and processing_authority_matches
    )
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
        "stored_source_fingerprint": stored_source_fingerprint or None,
        "expected_source_fingerprint": expected_fingerprint or None,
        "source_fingerprint_matches": source_fingerprint_matches,
        "processing_status": stored_processing_status or None,
        "stored_processing_authority_fingerprint": (
            stored_authority_fingerprint or None
        ),
        "expected_processing_authority_fingerprint": (
            expected_authority_fingerprint or None
        ),
        "processing_authority_matches": processing_authority_matches,
        "represented_artifacts_exist": represented_ids_exist,
        "source_processing_current": source_processing_current,
        # Keep the output contract stable when the marker has not been written
        # yet. Represented workflows can map the empty evidence object and
        # decide that the source is new without weakening writes-context
        # validation or inventing prior state.
        "source_processing_evidence": payload,
    }
    if text_relation_result is not None:
        response["source_processing_evidence_text_relation"] = dict(
            text_relation_result
        )
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
    source_fingerprint: str | None = None,
    processing_authority_fingerprint: str | None = None,
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
        expected_source_fingerprint=source_fingerprint,
        expected_processing_authority_fingerprint=(
            processing_authority_fingerprint
        ),
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
    source_fingerprint: str | None = None,
    processing_authority_fingerprint: str | None = None,
    processing_evidence: Mapping[str, Any] | None = None,
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

    marker_concept_id = source_processing_marker_concept_id(
        source_system=source_system_clean,
        source_profile=source_profile_clean,
        source_item_id=source_item_clean,
    )
    support_ids = [str(spec["concept_id"]) for spec in _SUPPORT_TYPE_SPECS]
    existing_ids = _find_existing_concept_ids([*support_ids, marker_concept_id])
    support_created = _ensure_support_concepts(existing_ids=existing_ids)
    marker_exists = marker_concept_id in existing_ids
    previous_payload = _load_marker_payload(marker_concept_id) if marker_exists else None
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
    fingerprint = _clean_text(source_fingerprint)
    previous_fingerprint = _clean_text(
        previous_payload.get("source_fingerprint")
        if isinstance(previous_payload, Mapping)
        else None
    )
    prior_history = (
        _as_sequence(previous_payload.get("source_fingerprint_history"))
        if isinstance(previous_payload, Mapping)
        else []
    )
    fingerprint_history = _ordered_unique(
        [*prior_history, previous_fingerprint, fingerprint]
    )[-20:]
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
        "source_fingerprint": fingerprint or None,
        "processing_authority_fingerprint": (
            _clean_text(processing_authority_fingerprint) or None
        ),
        "previous_source_fingerprint": (
            previous_fingerprint
            if previous_fingerprint and previous_fingerprint != fingerprint
            else None
        ),
        "source_fingerprint_history": fingerprint_history,
        "processing_evidence": (
            dict(processing_evidence)
            if isinstance(processing_evidence, Mapping)
            else {}
        ),
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
        expected_source_fingerprint=fingerprint or None,
        expected_processing_authority_fingerprint=(
            processing_authority_fingerprint
        ),
    )


__all__ = [
    "SOURCE_PROCESSING_EVIDENCE_PREDICATE_ID",
    "SOURCE_PROCESSING_MARKER_TYPE_ID",
    "get_source_processing_marker",
    "record_source_processing_marker",
    "source_processing_marker_concept_id",
]
