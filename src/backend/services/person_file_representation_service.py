"""Person file-copy representation persistence support.

Semantic interpretation lives in the shared Vontology-backed file-copy entity
representation prompt surface. This module keeps only persistence, verification,
and fail-closed handling for person candidates.
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
    normalise_file_copy_person_candidate,
)
from .relationship_write_service import add_relationship


def materialise_person_representation_for_file_copy(
    *,
    user_concept_id: str | None,
    file_copy_concept_id: str,
    extracted_text: str | None,
    original_filename: str | None = None,
    interpretation: Mapping[str, Any] | None = None,
    representation_candidate: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Materialise a person representation from an authority-backed candidate."""

    report: dict[str, Any] = {
        "success": False,
        "attempted": False,
        "verified": False,
        "reason": "not_person_artefact",
        "representation_mode": None,
        "file_copy_concept_id": file_copy_concept_id,
        "person_concept_id": None,
        "person_name": None,
        "emails": [],
        "phone_numbers": [],
        "affiliations": [],
        "roles": [],
        "created_person_concept": False,
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
        candidate = normalise_file_copy_person_candidate(
            payload.get("person_candidate")
        )
    else:
        candidate = normalise_file_copy_person_candidate(representation_candidate)

    if not candidate.get("applicable"):
        return report

    report["attempted"] = True
    report["representation_mode"] = candidate.get("representation_mode")

    actor_id = safe_str(user_concept_id)
    if not actor_id:
        report["reason"] = "missing_user_context_for_person_representation"
        return report

    person_name = safe_str(candidate.get("person_name"))
    emails = clean_string_list(candidate.get("emails"))
    phone_numbers = clean_string_list(candidate.get("phone_numbers"))
    affiliations = clean_string_list(candidate.get("affiliations"))
    roles = clean_string_list(candidate.get("roles"))

    report["person_name"] = person_name
    report["emails"] = list(emails)
    report["phone_numbers"] = list(phone_numbers)
    report["affiliations"] = list(affiliations)
    report["roles"] = list(roles)

    if not person_name:
        report["reason"] = "person_identity_unresolved"
        return report

    person_concept_id, created_person = resolve_or_create_named_instance_concept_id(
        user_concept_id=actor_id,
        entity_name=person_name,
        search_instance_type_ids=("#V#person",),
        create_instance_type_id="#V#person",
        prefix="person",
        system_tags=("person", "representation", "file_copy"),
        logger=logger,
        logger_label="person_file_representation",
    )
    report["person_concept_id"] = person_concept_id
    report["created_person_concept"] = created_person

    relation_specs: list[tuple[str, str, Mapping[str, Any]]] = [
        (
            "hasName",
            person_name,
            {
                "name_type": "NL",
                "source": "person_file_representation",
                "source_file_copy_concept_id": file_copy_concept_id,
            },
        )
    ]
    for email in emails:
        relation_specs.append(
            (
                "#V#has_email",
                email,
                {
                    "source": "person_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )
    for role in roles:
        relation_specs.append(
            (
                "#V#hasRole",
                role,
                {
                    "source": "person_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )
    for affiliation in affiliations:
        relation_specs.append(
            (
                "#V#hasNote",
                f"Affiliation: {affiliation}",
                {
                    "note_type": "affiliation",
                    "source": "person_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )
    for phone in phone_numbers:
        relation_specs.append(
            (
                "#V#hasNote",
                f"Phone: {phone}",
                {
                    "note_type": "phone",
                    "source": "person_file_representation",
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
                    "source": "person_file_representation",
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
                "source": "person_file_representation",
                "source_file_copy_concept_id": file_copy_concept_id,
            },
        )
    )

    name_persisted = False
    for predicate, value, context in relation_specs:
        relation, error = write_text_relation(
            subject_concept_id=person_concept_id,
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
        target=person_concept_id,
    )
    if isinstance(evidence_relation, Mapping) and evidence_relation.get("success") is True:
        report["persisted_structural_relations"].append(
            {
                "predicate": "#V#documentary_evidence_for",
                "source_id": file_copy_concept_id,
                "target_id": person_concept_id,
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
                "target_id": person_concept_id,
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
        report["reason"] = "person_representation_verified"
    elif not name_persisted:
        report["reason"] = "person_name_persist_failed"
    else:
        report["reason"] = "source_linkage_failed"
    return report


__all__ = ["materialise_person_representation_for_file_copy"]
