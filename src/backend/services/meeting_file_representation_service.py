"""Meeting file-copy representation persistence support.

Semantic interpretation lives in the shared Vontology-backed file-copy entity
representation prompt surface. This module keeps only persistence, verification,
and fail-closed handling for meeting candidates. RFC 5545 parsing is a
mechanical exception: a parsed iCalendar UID is an exact external identity, so
the deterministic ingestion support below may establish the core meeting and
source evidence without deciding that semantic representation is complete.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from . import concept_external_identity_service, concept_service
from .file_copy_entity_representation_support import (
    clean_string_list,
    resolve_or_create_named_instance_concept_id,
    safe_str,
    write_text_relation,
)
from .file_copy_entity_representation_vontology_service import (
    infer_file_copy_entity_representation_candidates,
    normalise_file_copy_meeting_candidate,
)
from .relationship_write_service import add_relationship
from .text_value_service import get_texts_for_concept

ICS_UID_IDENTIFIER_TYPE = "icalendar.uid"
ICS_MEETING_CONCEPT_ID_PREFIX = "meeting_ics"
MEETING_TYPE_ID = "#V#meeting"
DOCUMENTARY_EVIDENCE_PREDICATE_ID = "#V#documentary_evidence_for"


class IcsMeetingMaterialisationError(RuntimeError):
    """Raised when an exact ICS representation cannot be persisted safely."""

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})


def _object_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalise_targets(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Sequence[Any] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = value
    else:
        return ()
    return tuple(
        dict.fromkeys(
            cleaned for cleaned in (_clean_text(item) for item in values) if cleaned
        )
    )


def _concept_is_meeting(concept: Mapping[str, Any]) -> bool:
    relationships = concept.get("relationships")
    if not isinstance(relationships, Mapping):
        return False
    return MEETING_TYPE_ID in _normalise_targets(relationships.get("is_an_instance_of"))


def _load_exact_visible_concept(concept_id: str) -> dict[str, Any] | None:
    try:
        concept = concept_service.get_concept_by_concept_id_exact(concept_id)
    except concept_service.ConceptNotFoundError:
        return None
    except Exception as exc:
        raise IcsMeetingMaterialisationError(
            "ics_meeting_identity_read_failed",
            details={"exception_type": type(exc).__name__},
        ) from exc
    return dict(concept) if isinstance(concept, Mapping) else None


def _validate_uid_derived_meeting_concept(
    *, concept: Mapping[str, Any], concept_id: str, uid: str
) -> None:
    if not _concept_is_meeting(concept):
        raise IcsMeetingMaterialisationError(
            "ics_uid_concept_collision",
            details={"concept_id": concept_id, "reason": "not_a_meeting"},
        )
    existing_uid_rows = get_texts_for_concept(
        subject_concept_id=concept_id,
        predicate="hasName",
        limit=100,
        context_view="actor_effective",
    )
    typed_ics_uids: list[str] = []
    for row in existing_uid_rows:
        if not isinstance(row, Mapping):
            continue
        context = row.get("context")
        context = context if isinstance(context, Mapping) else {}
        if (
            _clean_text(context.get("identifier_type")).lower()
            != ICS_UID_IDENTIFIER_TYPE
        ):
            continue
        candidate_uid = _clean_text(row.get("text"))
        if candidate_uid:
            typed_ics_uids.append(candidate_uid)
    if typed_ics_uids and uid not in typed_ics_uids:
        raise IcsMeetingMaterialisationError(
            "ics_uid_concept_collision",
            details={
                "concept_id": concept_id,
                "reason": "different_icalendar_uid",
            },
        )
    if not typed_ics_uids:
        system_tags = {
            _clean_text(item).lower()
            for item in (concept.get("system_tags") or [])
            if _clean_text(item)
        }
        if "ics" not in system_tags:
            raise IcsMeetingMaterialisationError(
                "ics_uid_concept_collision",
                details={"concept_id": concept_id, "reason": "unowned_identity"},
            )


def _resolve_or_create_ics_meeting_concept(
    *,
    uid: str,
    summary: str,
    user_concept_id: str,
    organisation_concept_id: str | None,
    namespace: str | None,
) -> tuple[str, bool]:
    identifier = concept_external_identity_service.ExternalIdentifier(
        scheme=ICS_UID_IDENTIFIER_TYPE,
        value=uid,
        source="ics_meeting_representation",
    )
    try:
        resolution = (
            concept_external_identity_service.resolve_external_identity_candidates(
                identifier
            )
        )
    except Exception as exc:
        raise IcsMeetingMaterialisationError(
            "ics_uid_identity_resolution_failed",
            details={"exception_type": type(exc).__name__},
        ) from exc

    if resolution.status == "resolved":
        candidate_ids = tuple(resolution.candidate_concept_ids)
        if len(candidate_ids) != 1:
            raise IcsMeetingMaterialisationError(
                "ics_uid_identity_resolution_invalid",
                details={
                    "candidate_concept_ids": list(candidate_ids),
                    "resolution_source": resolution.resolution_source,
                },
            )
        concept_id = candidate_ids[0]
        existing = _load_exact_visible_concept(concept_id)
        if existing is None:
            raise IcsMeetingMaterialisationError(
                "ics_uid_identity_target_not_visible",
                details={
                    "concept_id": concept_id,
                    "resolution_source": resolution.resolution_source,
                },
            )
        if not _concept_is_meeting(existing):
            raise IcsMeetingMaterialisationError(
                "ics_uid_concept_collision",
                details={"concept_id": concept_id, "reason": "not_a_meeting"},
            )
        return concept_id, False

    if resolution.status != "not_found":
        raise IcsMeetingMaterialisationError(
            (
                "ics_uid_identity_ambiguous"
                if resolution.status == "ambiguous"
                else "ics_uid_identity_unverified"
            ),
            details={
                "identity_resolution_status": resolution.status,
                "candidate_concept_ids": list(resolution.candidate_concept_ids),
                "resolution_source": resolution.resolution_source,
            },
        )

    # Concept IDs are globally unique while meeting visibility is actor scoped.
    # Include the actor scope in the physical identity so two users importing
    # the same invitation cannot collide with an invisible concept.  UID
    # remains the semantic identity within the actor-visible search above.
    physical_identity = json.dumps(
        [user_concept_id, organisation_concept_id or "", uid],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    identity_digest = hashlib.sha256(physical_identity.encode("utf-8")).hexdigest()
    concept_id = f"#V#{ICS_MEETING_CONCEPT_ID_PREFIX}_{identity_digest}"
    existing = _load_exact_visible_concept(concept_id)
    if existing is not None:
        _validate_uid_derived_meeting_concept(
            concept=existing,
            concept_id=concept_id,
            uid=uid,
        )
        return concept_id, False

    try:
        concept_service.create_concept(
            name=summary,
            concept_id=concept_id,
            parent_concept_ids=[MEETING_TYPE_ID],
            create_as_instance=True,
            created_by_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            event_namespace=namespace,
            system_tags=["meeting", "representation", "file_copy", "ics"],
        )
    except Exception as exc:
        # An exact-ID reload closes the concurrent-create race without falling
        # back to a title match.  It also recovers a prior partial attempt whose
        # concept exists but whose UID text write did not finish.
        existing = _load_exact_visible_concept(concept_id)
        if existing is None:
            raise IcsMeetingMaterialisationError(
                "ics_meeting_concept_create_failed",
                details={"exception_type": type(exc).__name__},
            ) from exc
        _validate_uid_derived_meeting_concept(
            concept=existing,
            concept_id=concept_id,
            uid=uid,
        )
        return concept_id, False
    return concept_id, True


def _persist_ics_uid_identity_marker(
    *, concept_id: str, uid: str, created: bool
) -> dict[str, Any]:
    identifier = concept_external_identity_service.ExternalIdentifier(
        scheme=ICS_UID_IDENTIFIER_TYPE,
        value=uid,
        source="ics_meeting_representation",
    )
    try:
        persistence = (
            concept_external_identity_service.persist_external_identity_markers(
                concept_id=concept_id,
                identifiers=[identifier],
            )
        )
    except Exception as exc:
        raise IcsMeetingMaterialisationError(
            "ics_uid_identity_persist_failed",
            details={
                "meeting_concept_id": concept_id,
                "created_meeting_concept": created,
                "exception_type": type(exc).__name__,
            },
        ) from exc

    expected_marker = concept_external_identity_service.external_identity_marker(
        identifier
    )
    writes = persistence.get("writes") if isinstance(persistence, Mapping) else None
    marker_receipted = bool(
        isinstance(writes, Sequence)
        and not isinstance(writes, (str, bytes, bytearray))
        and any(
            isinstance(row, Mapping)
            and row.get("scheme") == ICS_UID_IDENTIFIER_TYPE
            and row.get("value") == uid
            and row.get("marker") == expected_marker
            for row in writes
        )
    )
    if (
        not isinstance(persistence, Mapping)
        or persistence.get("success") is not True
        or persistence.get("effect_status") != "succeeded"
        or not marker_receipted
    ):
        raise IcsMeetingMaterialisationError(
            "ics_uid_identity_persist_failed",
            details={
                "meeting_concept_id": concept_id,
                "created_meeting_concept": created,
                "identity_persistence": (
                    dict(persistence)
                    if isinstance(persistence, Mapping)
                    else {"response_type": type(persistence).__name__}
                ),
            },
        )
    return dict(persistence)


def _temporal_note(
    property_name: str, temporal: Any
) -> tuple[str, str, dict[str, Any]] | None:
    iso_value = _clean_text(_object_value(temporal, "iso_value"))
    if not iso_value:
        return None
    context: dict[str, Any] = {
        "note_type": f"ics_{property_name.lower()}",
        "icalendar_property": property_name,
        "source": "ics_meeting_representation",
    }
    for key in ("value_type", "tzid", "is_utc", "raw_value"):
        value = _object_value(temporal, key)
        if value is not None and value != "":
            context[key] = value
    return "#V#hasNote", f"{property_name}: {iso_value}", context


def _ics_fact_relation_specs(meeting: Any) -> list[tuple[str, str, dict[str, Any]]]:
    specs: list[tuple[str, str, dict[str, Any]]] = []
    for property_name, field_name in (("DTSTART", "dtstart"), ("DTEND", "dtend")):
        temporal_spec = _temporal_note(
            property_name, _object_value(meeting, field_name)
        )
        if temporal_spec is not None:
            specs.append(temporal_spec)

    for property_name, field_name in (
        ("LOCATION", "location"),
        ("DESCRIPTION", "description"),
        ("URL", "url"),
        ("STATUS", "status"),
    ):
        text = _clean_text(_object_value(meeting, field_name))
        if text:
            specs.append(
                (
                    "#V#hasNote",
                    f"{property_name}: {text}",
                    {
                        "note_type": f"ics_{field_name}",
                        "icalendar_property": property_name,
                        "source": "ics_meeting_representation",
                    },
                )
            )
    deduplicated: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for predicate, text, context in specs:
        fingerprint = (
            predicate,
            text,
            tuple(sorted((str(key), str(value)) for key, value in context.items())),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduplicated.append((predicate, text, context))
    return deduplicated


def _relationship_effect_receipt(
    *,
    source_id: str,
    predicate: str,
    target_id: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    modified = bool(
        result.get("forward_modified")
        if "forward_modified" in result
        else result.get("modified")
    )
    return {
        "tool": "add_relationship",
        "status": "ok",
        "effective_arguments": {
            "source_id": source_id,
            "predicate": predicate,
            "target": target_id,
        },
        "effective_payload": {
            "success": True,
            "effect_status": "succeeded",
            "relationship_type": "concept_relation",
            "source_id": source_id,
            "predicate": predicate,
            "predicate_input": predicate,
            "target": target_id,
            "added": modified,
            "changed": modified,
        },
        "service_result": dict(result),
    }


def materialise_ics_meeting_representation_for_file_copy(
    *,
    user_concept_id: str,
    organisation_concept_id: str | None,
    namespace: str | None,
    file_copy_concept_id: str,
    meeting: Any,
) -> dict[str, Any]:
    """Persist one already-parsed ICS event's core source facts by exact UID.

    This deliberately accepts a parser result rather than source text.  The
    parser owns RFC 5545 interpretation; this function owns actor-scoped exact
    identity, additive source persistence, and the real mutation receipt consumed
    by the workflow's independent canonical read-back. Participant identity and
    meeting-person links remain the represented workflow's responsibility.
    """

    actor_id = _clean_text(user_concept_id)
    source_id = _clean_text(file_copy_concept_id)
    uid = _clean_text(_object_value(meeting, "uid"))
    summary = _clean_text(_object_value(meeting, "summary"))
    if not actor_id:
        raise IcsMeetingMaterialisationError("ics_materialisation_actor_missing")
    if not source_id:
        raise IcsMeetingMaterialisationError("ics_materialisation_file_copy_missing")
    if not uid or not summary:
        raise IcsMeetingMaterialisationError("ics_materialisation_identity_missing")

    meeting_concept_id, created = _resolve_or_create_ics_meeting_concept(
        uid=uid,
        summary=summary,
        user_concept_id=actor_id,
        organisation_concept_id=_clean_text(organisation_concept_id) or None,
        namespace=_clean_text(namespace) or None,
    )
    identity_persistence = _persist_ics_uid_identity_marker(
        concept_id=meeting_concept_id,
        uid=uid,
        created=created,
    )

    relation_specs: list[tuple[str, str, Mapping[str, Any]]] = [
        (
            "hasName",
            uid,
            {
                "name_type": "CODE",
                "identifier_type": ICS_UID_IDENTIFIER_TYPE,
                "source": "ics_meeting_representation",
                "source_file_copy_concept_id": source_id,
            },
        ),
        (
            "hasName",
            summary,
            {
                "name_type": "NL",
                "source": "ics_meeting_representation",
                "source_file_copy_concept_id": source_id,
            },
        ),
        *_ics_fact_relation_specs(meeting),
    ]
    persisted_text_relations: list[dict[str, Any]] = []
    for predicate, text, raw_context in relation_specs:
        context = {
            **dict(raw_context),
            "source_file_copy_concept_id": source_id,
        }
        relation, error = write_text_relation(
            subject_concept_id=meeting_concept_id,
            predicate=predicate,
            text=text,
            context=context,
        )
        if error or not isinstance(relation, Mapping):
            raise IcsMeetingMaterialisationError(
                "ics_meeting_fact_persist_failed",
                details={
                    "meeting_concept_id": meeting_concept_id,
                    "predicate": predicate,
                    "error": error or "unexpected_text_relation_response",
                    "created_meeting_concept": created,
                    "persisted_text_relation_count": len(persisted_text_relations),
                },
            )
        persisted_text_relations.append(
            {
                "predicate": predicate,
                "relation_id": relation.get("relation_id"),
                "text": text,
            }
        )

    try:
        evidence_result = add_relationship(
            source_id=source_id,
            predicate=DOCUMENTARY_EVIDENCE_PREDICATE_ID,
            target=meeting_concept_id,
        )
    except Exception as exc:
        raise IcsMeetingMaterialisationError(
            "ics_documentary_evidence_persist_failed",
            details={
                "meeting_concept_id": meeting_concept_id,
                "created_meeting_concept": created,
                "persisted_text_relation_count": len(persisted_text_relations),
                "exception_type": type(exc).__name__,
            },
        ) from exc
    if (
        not isinstance(evidence_result, Mapping)
        or evidence_result.get("success") is not True
    ):
        raise IcsMeetingMaterialisationError(
            "ics_documentary_evidence_persist_failed",
            details={
                "meeting_concept_id": meeting_concept_id,
                "created_meeting_concept": created,
                "persisted_text_relation_count": len(persisted_text_relations),
                "relationship_result": (
                    dict(evidence_result)
                    if isinstance(evidence_result, Mapping)
                    else {"response_type": type(evidence_result).__name__}
                ),
            },
        )

    effect_receipt = _relationship_effect_receipt(
        source_id=source_id,
        predicate=DOCUMENTARY_EVIDENCE_PREDICATE_ID,
        target_id=meeting_concept_id,
        result=evidence_result,
    )
    operation = "created" if created else "updated"
    return {
        "success": True,
        "verified": False,
        "verification_pending": True,
        "reason": "ics_meeting_materialised_pending_canonical_readback",
        "meeting_concept_id": meeting_concept_id,
        "meeting_name": summary,
        "icalendar_uid": uid,
        "created_meeting_concept": created,
        "operation": operation,
        "external_identity_persistence": identity_persistence,
        "persisted_text_relations": persisted_text_relations,
        "persisted_structural_relations": [
            {
                "predicate": DOCUMENTARY_EVIDENCE_PREDICATE_ID,
                "source_id": source_id,
                "target_id": meeting_concept_id,
                "modified": bool(
                    evidence_result.get("forward_modified")
                    if "forward_modified" in evidence_result
                    else evidence_result.get("modified")
                ),
            }
        ],
        "relationship_effect_receipt": effect_receipt,
        "response_text": (
            f"{operation.title()} meeting '{summary}' as {meeting_concept_id} "
            "from the supplied iCalendar invitation, including its iCalendar "
            "UID, supported event facts, and documentary evidence."
        ),
    }


def materialise_meeting_representation_for_file_copy(
    *,
    user_concept_id: str | None,
    file_copy_concept_id: str,
    extracted_text: str | None,
    original_filename: str | None = None,
    interpretation: Mapping[str, Any] | None = None,
    representation_candidate: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Materialise a meeting representation from an authority-backed candidate."""

    report: dict[str, Any] = {
        "success": False,
        "attempted": False,
        "verified": False,
        "reason": "not_meeting_artefact",
        "representation_mode": None,
        "file_copy_concept_id": file_copy_concept_id,
        "meeting_concept_id": None,
        "meeting_name": None,
        "datetime_candidates": [],
        "participants": [],
        "outcomes": [],
        "created_meeting_concept": False,
        "persisted_text_relations": [],
        "persisted_structural_relations": [],
        "errors": [],
    }

    text = safe_str(extracted_text)
    if not text:
        report["reason"] = "no_extracted_text"
        return report

    if representation_candidate is None:
        payload, diagnostics = infer_file_copy_entity_representation_candidates(
            extracted_text=text,
            original_filename=original_filename,
            interpretation=interpretation,
        )
        report["candidate_diagnostics"] = diagnostics
        candidate = normalise_file_copy_meeting_candidate(
            payload.get("meeting_candidate")
        )
    else:
        candidate = normalise_file_copy_meeting_candidate(representation_candidate)

    if not candidate.get("applicable"):
        return report

    report["attempted"] = True
    report["representation_mode"] = candidate.get("representation_mode")

    actor_id = safe_str(user_concept_id)
    if not actor_id:
        report["reason"] = "missing_user_context_for_meeting_representation"
        return report

    meeting_name = safe_str(candidate.get("meeting_name"))
    datetime_candidates = clean_string_list(candidate.get("datetime_candidates"))
    participants = clean_string_list(candidate.get("participants"))
    outcomes = clean_string_list(candidate.get("outcomes"))

    report["meeting_name"] = meeting_name
    report["datetime_candidates"] = list(datetime_candidates)
    report["participants"] = list(participants)
    report["outcomes"] = list(outcomes)

    if not meeting_name:
        report["reason"] = "meeting_identity_unresolved"
        return report

    meeting_concept_id, created_meeting = resolve_or_create_named_instance_concept_id(
        user_concept_id=actor_id,
        entity_name=meeting_name,
        search_instance_type_ids=("#V#meeting",),
        create_instance_type_id="#V#meeting",
        prefix="meeting",
        system_tags=("meeting", "representation", "file_copy"),
        logger=logger,
        logger_label="meeting_file_representation",
    )
    report["meeting_concept_id"] = meeting_concept_id
    report["created_meeting_concept"] = created_meeting

    relation_specs: list[tuple[str, str, Mapping[str, Any]]] = [
        (
            "hasName",
            meeting_name,
            {
                "name_type": "NL",
                "source": "meeting_file_representation",
                "source_file_copy_concept_id": file_copy_concept_id,
            },
        )
    ]
    for value in datetime_candidates:
        relation_specs.append(
            (
                "#V#hasNote",
                f"Date/time: {value}",
                {
                    "note_type": "meeting_datetime",
                    "source": "meeting_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )
    for participant in participants:
        relation_specs.append(
            (
                "#V#hasNote",
                f"Participant: {participant}",
                {
                    "note_type": "meeting_participant",
                    "source": "meeting_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )
    for outcome in outcomes:
        relation_specs.append(
            (
                "#V#hasNote",
                f"Outcome/action: {outcome}",
                {
                    "note_type": "meeting_outcome",
                    "source": "meeting_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )

    interpretation_description = (
        safe_str(interpretation.get("description"))
        if isinstance(interpretation, Mapping)
        else None
    )
    if interpretation_description:
        relation_specs.append(
            (
                "#V#hasNote",
                f"Interpretation summary: {interpretation_description}",
                {
                    "note_type": "interpretation_summary",
                    "source": "meeting_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )

    relation_specs.append(
        (
            "#V#hasNote",
            f"Source file copy: {file_copy_concept_id}",
            {
                "note_type": "source_linkage",
                "source": "meeting_file_representation",
                "source_file_copy_concept_id": file_copy_concept_id,
            },
        )
    )

    name_persisted = False
    for predicate, value, context in relation_specs:
        relation, error = write_text_relation(
            subject_concept_id=meeting_concept_id,
            predicate=predicate,
            text=value,
            context=context,
        )
        if error:
            report["errors"].append(
                {
                    "stage": "text_relation_upsert",
                    "predicate": predicate,
                    "text": value,
                    "error": error,
                }
            )
            continue
        if predicate == "hasName":
            name_persisted = True
        report["persisted_text_relations"].append(
            {
                "predicate": predicate,
                "relation_id": (
                    relation.get("relation_id")
                    if isinstance(relation, Mapping)
                    else None
                ),
            }
        )

    evidence_relation = add_relationship(
        source_id=file_copy_concept_id,
        predicate="#V#documentary_evidence_for",
        target=meeting_concept_id,
    )
    if (
        isinstance(evidence_relation, Mapping)
        and evidence_relation.get("success") is True
    ):
        report["persisted_structural_relations"].append(
            {
                "predicate": "#V#documentary_evidence_for",
                "source_id": file_copy_concept_id,
                "target_id": meeting_concept_id,
                "modified": bool(evidence_relation.get("forward_modified")),
            }
        )
        evidence_linked = True
    else:
        evidence_linked = False
        report["errors"].append(
            {
                "stage": "source_evidence_link",
                "predicate": "#V#documentary_evidence_for",
                "source_id": file_copy_concept_id,
                "target_id": meeting_concept_id,
                "error": (
                    evidence_relation.get("error")
                    if isinstance(evidence_relation, Mapping)
                    else "unexpected_relationship_response"
                ),
                "details": evidence_relation,
            }
        )

    if name_persisted and evidence_linked:
        report["verified"] = True
        report["success"] = True
        report["reason"] = "meeting_representation_verified"
    elif not name_persisted:
        report["reason"] = "meeting_name_persist_failed"
    else:
        report["reason"] = "source_linkage_failed"
    return report


__all__ = [
    "IcsMeetingMaterialisationError",
    "materialise_ics_meeting_representation_for_file_copy",
    "materialise_meeting_representation_for_file_copy",
]
