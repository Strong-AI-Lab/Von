"""Meeting file-copy representation persistence support.

Semantic interpretation lives in the shared Vontology-backed file-copy entity
representation prompt surface. This module keeps only persistence, verification,
and fail-closed handling for meeting candidates.
"""

from __future__ import annotations

from typing import Any, Mapping

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
    if isinstance(evidence_relation, Mapping) and evidence_relation.get("success") is True:
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


__all__ = ["materialise_meeting_representation_for_file_copy"]
