from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from . import concept_search_service
from .relationship_write_service import add_relationship
from .text_value_service import upsert_text_for_concept

_MEETING_FILENAME_HINT_PATTERN = re.compile(
    r"\b(meeting|transcript|minutes|agenda|calendar|invite|invitation)\b",
    re.IGNORECASE,
)
_MEETING_TEXT_HINT_PATTERN = re.compile(
    r"\b(meeting|agenda|minutes|attendees|participants|calendar(?:\s+event)?|transcript|zoom|teams)\b",
    re.IGNORECASE,
)
_TRANSCRIPT_HINT_PATTERN = re.compile(r"\b(transcript|speaker|discussion)\b", re.IGNORECASE)
_CALENDAR_HINT_PATTERN = re.compile(r"\b(calendar|invite|invitation|ics|event)\b", re.IGNORECASE)
_MEETING_TITLE_LABEL_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:meeting|title|subject|event)\s*[:\-]\s*([^\n]{3,180})",
    re.IGNORECASE,
)
_ATTENDEES_LABEL_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:attendees|participants|guests)\s*[:\-]\s*([^\n]{2,400})",
    re.IGNORECASE,
)
_ISO_DATETIME_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+\-]\d{2}:\d{2})?)?\b"
)
_HUMAN_DATE_PATTERN = re.compile(
    r"\b(?:"
    r"(?:\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})"
    r"|(?:"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}"
    r")"
    r")"
    r"(?:\s+\d{1,2}:\d{2}(?:\s?(?:AM|PM))?)?\b",
    re.IGNORECASE,
)
_SPEAKER_LINE_PATTERN = re.compile(
    r"^(?:[-*]\s*)?([A-Z][A-Za-z'`.\-]+(?:\s+[A-Z][A-Za-z'`.\-]+){0,2})\s*:",
    re.IGNORECASE,
)
_GENERIC_TITLE_PATTERN = re.compile(
    r"^(meeting|meeting transcript|transcript|agenda|minutes|calendar event)$",
    re.IGNORECASE,
)
_NON_TITLE_HINT_PATTERN = re.compile(
    r"\b(email|phone|mobile|address|website|www\.|http)\b",
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


def _looks_like_meeting_title(candidate: str) -> bool:
    text = _normalise_line(candidate)
    if not text:
        return False
    if len(text) < 3 or len(text) > 180:
        return False
    if _GENERIC_TITLE_PATTERN.fullmatch(text):
        return False
    if _NON_TITLE_HINT_PATTERN.search(text):
        return False
    if _ISO_DATETIME_PATTERN.search(text):
        return False
    if sum(char.isalpha() for char in text) < 3:
        return False
    return True


def _extract_meeting_titles(*, text: str, lines: list[str]) -> list[str]:
    candidates: list[str] = []

    for match in _MEETING_TITLE_LABEL_PATTERN.finditer(text):
        candidate = _normalise_line(match.group(1))
        if _looks_like_meeting_title(candidate):
            candidates.append(candidate)

    for line in lines[:8]:
        if _looks_like_meeting_title(line):
            candidates.append(line)

    return _dedupe_casefold(candidates)


def _extract_datetime_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in _ISO_DATETIME_PATTERN.finditer(text):
        candidates.append(_normalise_line(match.group(0)))
    for match in _HUMAN_DATE_PATTERN.finditer(text):
        candidates.append(_normalise_line(match.group(0)))
    return _dedupe_casefold(candidates)[:3]


def _looks_like_participant_name(candidate: str) -> bool:
    text = _normalise_line(candidate)
    if not text:
        return False
    if len(text) < 3 or len(text) > 100:
        return False
    if any(char.isdigit() for char in text):
        return False
    if _NON_TITLE_HINT_PATTERN.search(text):
        return False
    tokens = [token for token in _SEPARATOR_PATTERN.split(text) if token]
    if len(tokens) < 1 or len(tokens) > 4:
        return False
    for token in tokens:
        if len(token) < 2:
            return False
        if not re.fullmatch(r"[A-Za-z'`.\-]+", token):
            return False
    return True


def _extract_participants(*, text: str, lines: list[str]) -> list[str]:
    participants: list[str] = []

    for match in _ATTENDEES_LABEL_PATTERN.finditer(text):
        raw = _normalise_line(match.group(1))
        for part in re.split(r"[;,]|(?:\band\b)", raw):
            candidate = _normalise_line(part)
            if _looks_like_participant_name(candidate):
                participants.append(candidate)

    for line in lines:
        speaker_match = _SPEAKER_LINE_PATTERN.match(line)
        if speaker_match:
            candidate = _normalise_line(speaker_match.group(1))
            if _looks_like_participant_name(candidate):
                participants.append(candidate)

    return _dedupe_casefold(participants)[:10]


def _extract_outcome_lines(lines: list[str]) -> list[str]:
    outcomes: list[str] = []
    outcome_pattern = re.compile(
        r"\b(action(?:\s+item)?|decision|outcome|follow[\s\-]?up|next steps?)\b",
        re.IGNORECASE,
    )
    for line in lines:
        if outcome_pattern.search(line):
            outcomes.append(line)
    return _dedupe_casefold(outcomes)[:5]


def _infer_representation_mode(
    *,
    original_filename: str | None,
    text: str,
    lines: list[str],
) -> str | None:
    filename = (original_filename or "").strip().lower()
    if filename and _MEETING_FILENAME_HINT_PATTERN.search(filename):
        if _TRANSCRIPT_HINT_PATTERN.search(filename) or any(
            _SPEAKER_LINE_PATTERN.match(line or "") for line in lines[:20]
        ):
            return "transcript"
        if _CALENDAR_HINT_PATTERN.search(filename):
            return "calendar"
        return "meeting"

    if _MEETING_TEXT_HINT_PATTERN.search(text):
        if _TRANSCRIPT_HINT_PATTERN.search(text) or any(
            _SPEAKER_LINE_PATTERN.match(line or "") for line in lines[:30]
        ):
            return "transcript"
        if _CALENDAR_HINT_PATTERN.search(text):
            return "calendar"
        return "meeting"
    return None


def _stable_named_instance_concept_id(name: str, *, prefix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if not slug:
        slug = "unnamed"
    digest = hashlib.sha256(str(name or "").strip().casefold().encode("utf-8")).hexdigest()[
        :8
    ]
    return f"#V#{prefix}_{slug}_{digest}"


def _resolve_or_create_meeting_concept_id(
    *,
    user_concept_id: str,
    meeting_name: str,
    logger: Any | None = None,
) -> tuple[str, bool]:
    from . import concept_service

    search_result = concept_search_service.search_concepts(
        query=meeting_name,
        instance_of="#V#meeting",
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
        if candidate_name and candidate_name.casefold() == meeting_name.casefold():
            return candidate_id, False

    concept_id = _stable_named_instance_concept_id(meeting_name, prefix="meeting")
    try:
        concept_service.create_concept(
            name=meeting_name,
            concept_id=concept_id,
            parent_concept_ids=["#V#meeting"],
            create_as_instance=True,
            system_tags=["meeting", "representation", "file_copy"],
        )
        concept_service.update_concept(
            concept_id,
            {"relationships.specific_to_user": [user_concept_id.strip()]},
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[meeting_file_representation] Meeting concept create failed for %s: %s",
                meeting_name,
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


def materialise_meeting_representation_for_file_copy(
    *,
    user_concept_id: str | None,
    file_copy_concept_id: str,
    extracted_text: str | None,
    original_filename: str | None = None,
    interpretation: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Deterministically materialise a meeting representation from transcript/calendar artefacts."""

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

    text = _safe_str(extracted_text)
    if not text:
        report["reason"] = "no_extracted_text"
        return report

    lines = _extract_lines(text)
    representation_mode = _infer_representation_mode(
        original_filename=original_filename,
        text=text,
        lines=lines,
    )
    if representation_mode is None:
        return report

    report["attempted"] = True
    report["representation_mode"] = representation_mode

    actor_id = _safe_str(user_concept_id)
    if not actor_id:
        report["reason"] = "missing_user_context_for_meeting_representation"
        return report

    title_candidates = _extract_meeting_titles(text=text, lines=lines)
    datetime_candidates = _extract_datetime_candidates(text)
    participants = _extract_participants(text=text, lines=lines)
    outcomes = _extract_outcome_lines(lines)

    meeting_name = title_candidates[0] if title_candidates else None
    if not meeting_name and datetime_candidates:
        meeting_name = f"Meeting on {datetime_candidates[0]}"

    report["meeting_name"] = meeting_name
    report["datetime_candidates"] = list(datetime_candidates)
    report["participants"] = list(participants)
    report["outcomes"] = list(outcomes)

    if not meeting_name:
        report["reason"] = "meeting_identity_unresolved"
        return report

    meeting_concept_id, created_meeting = _resolve_or_create_meeting_concept_id(
        user_concept_id=actor_id,
        meeting_name=meeting_name,
        logger=logger,
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
        relation, error = _write_text_relation(
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
                "relation_id": relation.get("relation_id") if isinstance(relation, Mapping) else None,
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
