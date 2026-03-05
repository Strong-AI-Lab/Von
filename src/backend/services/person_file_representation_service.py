from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from . import concept_search_service
from .relationship_write_service import add_relationship
from .text_value_service import upsert_text_for_concept

_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_PATTERN = re.compile(
    r"(?:\+?\d[\d()\s\-]{7,}\d)",
    re.IGNORECASE,
)
_CV_HINT_PATTERN = re.compile(
    r"\b(cv|resume|curriculum[\s\-_]*vitae)\b",
    re.IGNORECASE,
)
_BUSINESS_CARD_HINT_PATTERN = re.compile(
    r"\b(business[\s\-_]*card|biz[\s\-_]*card|contact[\s\-_]*card)\b",
    re.IGNORECASE,
)
_NAME_LABEL_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:name|full\s+name|candidate|contact)\s*[:\-]\s*([^\n]{2,120})",
    re.IGNORECASE,
)
_AFFILIATION_LABEL_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:affiliation|organisation|organization|company|institution)\s*[:\-]\s*([^\n]{2,200})",
    re.IGNORECASE,
)
_ROLE_LABEL_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:role|title|position)\s*[:\-]\s*([^\n]{2,120})",
    re.IGNORECASE,
)
_AFFILIATION_LINE_HINT_PATTERN = re.compile(
    r"\b(university|institute|organisation|organization|company|department|school|laboratory|lab|group|inc|ltd|llc)\b",
    re.IGNORECASE,
)
_ROLE_LINE_HINT_PATTERN = re.compile(
    r"\b(professor|dr\.?|researcher|engineer|scientist|director|manager|student|lecturer|founder|ceo|cto|coo)\b",
    re.IGNORECASE,
)
_NON_NAME_HINT_PATTERN = re.compile(
    r"\b(email|phone|mobile|address|website|www\.|http|curriculum|resume|business|card)\b",
    re.IGNORECASE,
)
_SEPARATOR_PATTERN = re.compile(r"[\s._\-]+")


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _dedupe_casefold(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def _normalise_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t\r\n;|")


def _extract_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        normalised = _normalise_line(raw_line)
        if not normalised:
            continue
        lines.append(normalised)
    return lines


def _normalise_person_name(value: Any) -> str:
    text = _safe_str(value)
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_person_name(candidate: str) -> bool:
    text = _normalise_person_name(candidate)
    if not text:
        return False
    if len(text) < 3 or len(text) > 120:
        return False
    if _NON_NAME_HINT_PATTERN.search(text):
        return False
    if any(char.isdigit() for char in text):
        return False

    tokens = [token for token in _SEPARATOR_PATTERN.split(text) if token]
    if len(tokens) < 2 or len(tokens) > 5:
        return False
    for token in tokens:
        if len(token) < 2:
            return False
        if not re.fullmatch(r"[A-Za-z'`]+", token):
            return False
    return True


def _extract_emails(text: str) -> list[str]:
    return _dedupe_casefold([match.group(0) for match in _EMAIL_PATTERN.finditer(text)])


def _extract_phone_numbers(text: str) -> list[str]:
    numbers: list[str] = []
    for match in _PHONE_PATTERN.finditer(text):
        value = _normalise_line(match.group(0))
        digit_count = sum(char.isdigit() for char in value)
        if digit_count < 8:
            continue
        numbers.append(value)
    return _dedupe_casefold(numbers)


def _name_from_email(email: str) -> str | None:
    local = email.split("@", 1)[0].strip()
    if not local:
        return None
    parts = [part for part in _SEPARATOR_PATTERN.split(local) if part]
    if len(parts) < 2 or len(parts) > 4:
        return None
    candidate = " ".join(part.capitalize() for part in parts)
    return candidate if _looks_like_person_name(candidate) else None


def _extract_candidate_names(*, text: str, lines: list[str], emails: list[str]) -> list[str]:
    candidates: list[str] = []

    for match in _NAME_LABEL_PATTERN.finditer(text):
        candidate = _normalise_line(match.group(1))
        if _looks_like_person_name(candidate):
            candidates.append(candidate)

    for line in lines[:8]:
        if _looks_like_person_name(line):
            candidates.append(line)

    for email in emails:
        fallback = _name_from_email(email)
        if fallback:
            candidates.append(fallback)

    return _dedupe_casefold(candidates)


def _extract_affiliations(*, text: str, lines: list[str]) -> list[str]:
    affiliations: list[str] = []

    for match in _AFFILIATION_LABEL_PATTERN.finditer(text):
        value = _normalise_line(match.group(1))
        if value:
            affiliations.append(value)

    for line in lines:
        if _EMAIL_PATTERN.search(line) or _PHONE_PATTERN.search(line):
            continue
        if _AFFILIATION_LINE_HINT_PATTERN.search(line):
            affiliations.append(line)

    return _dedupe_casefold(affiliations)[:3]


def _extract_roles(*, text: str, lines: list[str]) -> list[str]:
    roles: list[str] = []

    for match in _ROLE_LABEL_PATTERN.finditer(text):
        value = _normalise_line(match.group(1))
        if value:
            roles.append(value)

    for line in lines:
        if _ROLE_LINE_HINT_PATTERN.search(line):
            roles.append(line)

    return _dedupe_casefold(roles)[:3]


def _infer_representation_mode(
    *,
    original_filename: str | None,
    text: str,
    lines: list[str],
    emails: list[str],
    phone_numbers: list[str],
) -> str | None:
    filename = (original_filename or "").strip().lower()
    if filename and _CV_HINT_PATTERN.search(filename):
        return "cv"
    if filename and _BUSINESS_CARD_HINT_PATTERN.search(filename):
        return "business_card"

    if _CV_HINT_PATTERN.search(text):
        return "cv"
    if _BUSINESS_CARD_HINT_PATTERN.search(text):
        return "business_card"

    # Business cards are often short OCR snippets containing contact fields.
    if len(lines) <= 24 and emails and phone_numbers:
        return "business_card"
    return None


def _stable_named_instance_concept_id(name: str, *, prefix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if not slug:
        slug = "unnamed"
    digest = hashlib.sha256(str(name or "").strip().casefold().encode("utf-8")).hexdigest()[
        :8
    ]
    return f"#V#{prefix}_{slug}_{digest}"


def _resolve_or_create_person_concept_id(
    *,
    user_concept_id: str,
    person_name: str,
    logger: Any | None = None,
) -> tuple[str, bool]:
    from . import concept_service

    search_result = concept_search_service.search_concepts(
        query=person_name,
        instance_of="#V#person",
        match_type="exact",
        limit=10,
    )
    for item in search_result.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        candidate_id = _safe_str(item.get("concept_id"))
        candidate_name = _safe_str(item.get("name"))
        if not candidate_id:
            continue
        if candidate_name and candidate_name.casefold() == person_name.casefold():
            return candidate_id, False

    concept_id = _stable_named_instance_concept_id(person_name, prefix="person")
    try:
        concept_service.create_concept(
            name=person_name,
            concept_id=concept_id,
            parent_concept_ids=["#V#person"],
            create_as_instance=True,
            system_tags=["person", "representation", "file_copy"],
        )
        concept_service.update_concept(
            concept_id,
            {"relationships.specific_to_user": [user_concept_id.strip()]},
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[person_file_representation] Person concept create failed for %s: %s",
                person_name,
                exc,
            )
    return concept_id, True


def _write_text_relation(
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


def materialise_person_representation_for_file_copy(
    *,
    user_concept_id: str | None,
    file_copy_concept_id: str,
    extracted_text: str | None,
    original_filename: str | None = None,
    interpretation: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Deterministically materialise a person representation from CV/card artefacts."""

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

    text = _safe_str(extracted_text)
    if not text:
        report["reason"] = "no_extracted_text"
        return report

    lines = _extract_lines(text)
    emails = _extract_emails(text)
    phone_numbers = _extract_phone_numbers(text)
    representation_mode = _infer_representation_mode(
        original_filename=original_filename,
        text=text,
        lines=lines,
        emails=emails,
        phone_numbers=phone_numbers,
    )
    if representation_mode is None:
        return report

    report["attempted"] = True
    report["representation_mode"] = representation_mode
    report["emails"] = list(emails)
    report["phone_numbers"] = list(phone_numbers)

    actor_id = _safe_str(user_concept_id)
    if not actor_id:
        report["reason"] = "missing_user_context_for_person_representation"
        return report

    name_candidates = _extract_candidate_names(text=text, lines=lines, emails=emails)
    person_name = name_candidates[0] if name_candidates else None
    if not person_name:
        report["reason"] = "person_identity_unresolved"
        return report

    affiliations = _extract_affiliations(text=text, lines=lines)
    roles = _extract_roles(text=text, lines=lines)
    report["person_name"] = person_name
    report["affiliations"] = list(affiliations)
    report["roles"] = list(roles)

    person_concept_id, created_person = _resolve_or_create_person_concept_id(
        user_concept_id=actor_id,
        person_name=person_name,
        logger=logger,
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
        _safe_str(interpretation.get("description"))
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
        relation, error = _write_text_relation(
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
                "relation_id": relation.get("relation_id") if isinstance(relation, Mapping) else None,
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
