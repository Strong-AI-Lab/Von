"""Grounded paper recommendation ranking over represented scholarly papers.

This service deliberately treats recommendation as a thin, inspectable layer
over authoritative Vontology substrate:

- the per-user paper recommendation profile from JVNAUTOSCI-117
- represented scholarly-paper concepts and their attached metadata/relations

It returns transient recommendation result objects for the current task. Later
delivery/review work can persist or surface those results without re-defining
the ranking logic.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Iterable, Mapping, Sequence, cast

PAPER_RECOMMENDATION_POLICY_VERSION = "paper_recommendation_policy.v1"
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_ARXIV_ID_RE = re.compile(r"^(?:\d{4}\.\d{4,5}(?:v\d+)?)$", re.IGNORECASE)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_LOW_INFORMATION_TOKENS = frozenset(
    {
        "a",
        "an",
        "and",
        "approach",
        "approaches",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "method",
        "methods",
        "model",
        "models",
        "of",
        "on",
        "or",
        "our",
        "paper",
        "papers",
        "project",
        "projects",
        "research",
        "study",
        "studies",
        "system",
        "systems",
        "that",
        "the",
        "their",
        "this",
        "to",
        "using",
        "we",
        "with",
        "work",
        "works",
    }
)
_PREFERRED_LANGUAGE_ORDER = ("en-nz", "en")
_VENUE_PREDICATE_HINTS = (
    "venue",
    "journal",
    "conference",
    "proceedings",
    "published_in",
    "publishedin",
)
_HAS_NAME_PREDICATES = frozenset({"hasname", "#v#hasname"})
_HAS_DESCRIPTION_PREDICATES = frozenset({"hasdescription", "#v#hasdescription"})


def _normalise_predicate(value: Any) -> str:
    return _safe_text(value).casefold()


def _as_str_mapping(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[str, Any], value)


def _coerce_mapping_dict(value: Any) -> dict[str, Any]:
    mapping = _as_str_mapping(value)
    if mapping is None:
        return {}
    return {
        str(key): item
        for key, item in mapping.items()
        if isinstance(key, str) and key.strip()
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_concept_by_concept_id(concept_id: str):
    from .concept_service import get_concept_by_concept_id as _impl

    return _impl(concept_id)


def load_paper_recommendation_profile(*, user_concept_id: str, create_if_missing: bool):
    from .paper_recommendation_profile_vontology_service import (
        load_paper_recommendation_profile as _impl,
    )

    return _impl(
        user_concept_id=user_concept_id,
        create_if_missing=create_if_missing,
    )


def get_texts_for_concept(
    subject_concept_id: str,
    predicate: str | None = None,
    limit: int = 50,
):
    from .text_value_service import get_texts_for_concept as _impl

    return _impl(
        subject_concept_id=subject_concept_id,
        predicate=predicate,
        limit=limit,
    )


def _safe_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _dedupe_casefold(values: Iterable[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _safe_text(value)
        if not cleaned:
            continue
        fingerprint = cleaned.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(cleaned)
    return deduped


def _normalise_token_variants(token: str) -> list[str]:
    cleaned = _safe_text(token).lower()
    if not cleaned:
        return []

    variants = [cleaned]
    singular = cleaned
    if cleaned.endswith("ies") and len(cleaned) > 4:
        singular = cleaned[:-3] + "y"
    elif (
        (cleaned.endswith("es") and len(cleaned) > 4 and cleaned[-3:-2] in {"s", "x", "z"})
        or cleaned.endswith(("ches", "shes"))
    ):
        singular = cleaned[:-2]
    elif cleaned.endswith("s") and len(cleaned) > 4 and not cleaned.endswith(
        ("ss", "us", "is")
    ):
        singular = cleaned[:-1]

    singular = singular.strip()
    if singular and singular not in variants:
        variants.append(singular)
    return variants


def _tokenise(text: Any) -> list[str]:
    source = _safe_text(text).lower()
    if not source:
        return []

    tokens: list[str] = []
    for raw_token in _TOKEN_RE.findall(source):
        for variant in _normalise_token_variants(raw_token):
            if variant in _LOW_INFORMATION_TOKENS or len(variant) <= 2:
                continue
            tokens.append(variant)
    return tokens


def _extract_keyword_terms(text: Any, *, max_terms: int = 12) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in _tokenise(text):
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= max_terms:
            break
    return terms


def _normalise_phrase(value: Any) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().casefold()


def _looks_like_code_name(text: str) -> bool:
    cleaned = _safe_text(text)
    if not cleaned:
        return True
    lowered = cleaned.casefold()
    if lowered.startswith("#v#"):
        return True
    if _URL_RE.match(cleaned):
        return True
    if _ARXIV_ID_RE.match(cleaned):
        return True
    return False


def _pick_preferred_title(name_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    ranked: list[tuple[int, int, int, dict[str, Any]]] = []
    fallback: list[tuple[int, int, dict[str, Any]]] = []
    for row in name_rows:
        if not isinstance(row, Mapping):
            continue
        text = _safe_text(row.get("text"))
        if not text:
            continue
        context = _coerce_mapping_dict(row.get("context"))
        name_type = _safe_text(context.get("name_type")).upper()
        lang = _safe_text(row.get("lang")).lower()
        lang_rank = 0 if lang in _PREFERRED_LANGUAGE_ORDER else 1
        if name_type == "CODE" or _looks_like_code_name(text):
            fallback.append((lang_rank, len(text), dict(row)))
            continue
        type_rank = 0 if name_type == "NL" else 1
        ranked.append((type_rank, lang_rank, len(text), dict(row)))
    if ranked:
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        return ranked[0][3]
    if fallback:
        fallback.sort(key=lambda item: (item[0], item[1]))
        return fallback[0][2]
    return None


def _pick_preferred_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = _safe_text(row.get("text"))
        if not text:
            continue
        predicate = _normalise_predicate(row.get("predicate"))
        lang = _safe_text(row.get("lang")).lower()
        lang_rank = 0 if lang in _PREFERRED_LANGUAGE_ORDER else 1
        if predicate in _HAS_DESCRIPTION_PREDICATES:
            predicate_rank = 0
        elif "abstract" in predicate or "summary" in predicate:
            predicate_rank = 1
        elif predicate == "hascontent":
            predicate_rank = 2
        else:
            predicate_rank = 3
        candidates.append((predicate_rank, lang_rank, dict(row)))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return _dedupe_casefold(part.strip() for part in value.split(","))
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return _dedupe_casefold(str(item) for item in value if isinstance(item, str))


def _load_concept_or_none(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept_doc = get_concept_by_concept_id(concept_id)
    except Exception:
        return None
    return concept_doc if isinstance(concept_doc, Mapping) else None


def _display_name_for_concept(concept_doc: Mapping[str, Any] | None, concept_id: str) -> str:
    name_rows = get_texts_for_concept(concept_id, predicate="hasName", limit=20)
    preferred_name_row = _pick_preferred_title(name_rows)
    if isinstance(preferred_name_row, Mapping):
        preferred_name = _safe_text(preferred_name_row.get("text"))
        if preferred_name:
            return preferred_name

    if isinstance(concept_doc, Mapping):
        top_name = _safe_text(concept_doc.get("name"))
        if top_name:
            return top_name
    return concept_id


def _extract_related_concept_entries(relationship_targets: Any) -> list[dict[str, str]]:
    if isinstance(relationship_targets, str):
        target_ids = [relationship_targets]
    elif isinstance(relationship_targets, list):
        target_ids = [str(item) for item in relationship_targets if isinstance(item, str)]
    else:
        target_ids = []

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for target_id in target_ids:
        cleaned_id = _safe_text(target_id)
        if not cleaned_id or cleaned_id in seen:
            continue
        seen.add(cleaned_id)
        concept_doc = _load_concept_or_none(cleaned_id)
        results.append(
            {
                "concept_id": cleaned_id,
                "name": _display_name_for_concept(concept_doc, cleaned_id),
            }
        )
    return results


def _extract_paper_venues(text_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    venues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in text_rows:
        if not isinstance(row, Mapping):
            continue
        predicate = _normalise_predicate(row.get("predicate"))
        if not any(hint in predicate for hint in _VENUE_PREDICATE_HINTS):
            continue
        text = _safe_text(row.get("text"))
        if not text:
            continue
        fingerprint = text.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        venues.append(
            {
                "text": text,
                "predicate": row.get("predicate"),
                "relation_id": row.get("relation_id"),
            }
        )
    return venues


def _build_profile_signals(*, profile_payload: Mapping[str, Any]) -> dict[str, Any]:
    profile = _coerce_mapping_dict(profile_payload.get("profile"))
    derived_context = _coerce_mapping_dict(profile_payload.get("derived_context"))

    stated_interest_terms = _coerce_string_list(profile.get("stated_interest_terms"))
    negative_interest_terms = _coerce_string_list(profile.get("negative_interest_terms"))
    preferred_authors = _coerce_string_list(profile.get("preferred_authors"))
    preferred_venues = _coerce_string_list(profile.get("preferred_venues"))

    research_interest_names = _dedupe_casefold(
        _safe_text(item.get("name"))
        for item in (derived_context.get("research_interest_concepts") or [])
        if isinstance(item, Mapping)
    )

    project_description_keywords = _extract_keyword_terms(
        profile.get("project_description")
    )
    notes_keywords = _extract_keyword_terms(profile.get("notes"), max_terms=8)

    usable = bool(
        stated_interest_terms
        or research_interest_names
        or preferred_authors
        or preferred_venues
        or project_description_keywords
        or notes_keywords
    )

    return {
        "profile": profile,
        "profile_concept_id": _safe_text(profile_payload.get("profile_concept_id")),
        "diagnostics": _coerce_mapping_dict(profile_payload.get("diagnostics")),
        "stated_interest_terms": stated_interest_terms,
        "negative_interest_terms": negative_interest_terms,
        "preferred_authors": preferred_authors,
        "preferred_venues": preferred_venues,
        "research_interest_names": research_interest_names,
        "project_description_keywords": project_description_keywords,
        "notes_keywords": notes_keywords,
        "signal_summary": {
            "stated_interest_term_count": len(stated_interest_terms),
            "negative_interest_term_count": len(negative_interest_terms),
            "preferred_author_count": len(preferred_authors),
            "preferred_venue_count": len(preferred_venues),
            "derived_research_interest_count": len(research_interest_names),
            "project_description_keyword_count": len(project_description_keywords),
            "notes_keyword_count": len(notes_keywords),
            "usable": usable,
        },
    }


def _build_paper_representation(paper_concept_id: str) -> dict[str, Any]:
    concept_doc = _load_concept_or_none(paper_concept_id)
    if concept_doc is None:
        return {
            "paper_concept_id": paper_concept_id,
            "status": "missing_candidate_paper",
            "representation_complete": False,
            "representation_failures": ["paper_concept_missing"],
        }

    text_rows = get_texts_for_concept(paper_concept_id, limit=200)
    name_rows = [
        row
        for row in text_rows
        if _normalise_predicate(row.get("predicate")) in _HAS_NAME_PREDICATES
    ]
    summary_candidate_rows = [
        row
        for row in text_rows
        if _normalise_predicate(row.get("predicate")) not in _HAS_NAME_PREDICATES
    ]
    summary_row = _pick_preferred_summary(summary_candidate_rows)
    title_row = _pick_preferred_title(name_rows)
    publication_rows = [
        row
        for row in text_rows
        if _safe_text(row.get("predicate")) == "#V#has_publication_date"
    ]
    publication_row = publication_rows[0] if publication_rows else None
    topic_label_rows = [
        row
        for row in text_rows
        if _safe_text(row.get("predicate")) == "#V#has_topic_labels"
    ]

    relationships = _coerce_mapping_dict(concept_doc.get("relationships"))
    author_entries = _extract_related_concept_entries(relationships.get("#V#authored_by"))
    topic_entries = _extract_related_concept_entries(relationships.get("#V#about"))
    venue_entries = _extract_paper_venues(text_rows)

    topic_label_texts = _dedupe_casefold(
        [
            *[str(item.get("name")) for item in topic_entries if isinstance(item, Mapping)],
            *[
                part.strip()
                for row in topic_label_rows
                for part in _safe_text(row.get("text")).split(",")
                if part.strip()
            ],
        ]
    )
    author_names = _dedupe_casefold(
        item.get("name") for item in author_entries if isinstance(item, Mapping)
    )
    venue_names = _dedupe_casefold(
        item.get("text") for item in venue_entries if isinstance(item, Mapping)
    )

    title = _safe_text(title_row.get("text")) if isinstance(title_row, Mapping) else ""
    if not title:
        title = _display_name_for_concept(concept_doc, paper_concept_id)
    summary = _safe_text(summary_row.get("text")) if isinstance(summary_row, Mapping) else ""
    publication_date = (
        _safe_text(publication_row.get("text"))
        if isinstance(publication_row, Mapping)
        else ""
    )

    combined_text = " ".join(
        part
        for part in (
            title,
            summary,
            " ".join(topic_label_texts),
            " ".join(author_names),
            " ".join(venue_names),
        )
        if part
    )
    token_set = set(_tokenise(combined_text))

    failures: list[str] = []
    if not title:
        failures.append("title_missing")
    if not summary and not topic_label_texts and not author_names and not venue_names:
        failures.append("insufficient_paper_metadata")

    return {
        "paper_concept_id": paper_concept_id,
        "paper_title": title,
        "paper_summary": summary,
        "summary_excerpt": summary[:280] if summary else "",
        "author_names": author_names,
        "topic_labels": topic_label_texts,
        "venue_names": venue_names,
        "publication_date": publication_date or None,
        "token_set": sorted(token_set),
        "representation_complete": len(failures) == 0,
        "representation_failures": failures,
        "provenance": {
            "paper_concept_id": paper_concept_id,
            "title_relation_id": title_row.get("relation_id")
            if isinstance(title_row, Mapping)
            else None,
            "summary_relation_id": summary_row.get("relation_id")
            if isinstance(summary_row, Mapping)
            else None,
            "publication_date_relation_id": publication_row.get("relation_id")
            if isinstance(publication_row, Mapping)
            else None,
            "author_concept_ids": [
                item.get("concept_id") for item in author_entries if isinstance(item, Mapping)
            ],
            "topic_concept_ids": [
                item.get("concept_id") for item in topic_entries if isinstance(item, Mapping)
            ],
            "topic_label_relation_ids": [
                row.get("relation_id")
                for row in topic_label_rows
                if isinstance(row, Mapping)
            ],
            "venue_relation_ids": [
                item.get("relation_id") for item in venue_entries if isinstance(item, Mapping)
            ],
        },
    }


def _field_match(term: str, *, field_text: str, field_tokens: set[str]) -> bool:
    normalised_term = _normalise_phrase(term)
    if not normalised_term:
        return False
    normalised_field = _normalise_phrase(field_text)
    if normalised_term and normalised_term in normalised_field:
        return True
    term_tokens = set(_tokenise(term))
    if term_tokens and term_tokens.issubset(field_tokens):
        return True
    return False


def _find_textual_match(term: str, *, paper: Mapping[str, Any]) -> dict[str, Any] | None:
    field_specs = (
        (
            "paper.topic_labels",
            list(paper.get("topic_labels") or []),
            list((paper.get("provenance") or {}).get("topic_concept_ids") or []),
        ),
        (
            "paper.title",
            [_safe_text(paper.get("paper_title"))],
            [
                _safe_text((paper.get("provenance") or {}).get("title_relation_id"))
                or None
            ],
        ),
        (
            "paper.summary",
            [_safe_text(paper.get("paper_summary"))],
            [
                _safe_text((paper.get("provenance") or {}).get("summary_relation_id"))
                or None
            ],
        ),
        (
            "paper.venues",
            list(paper.get("venue_names") or []),
            list((paper.get("provenance") or {}).get("venue_relation_ids") or []),
        ),
        (
            "paper.authors",
            list(paper.get("author_names") or []),
            list((paper.get("provenance") or {}).get("author_concept_ids") or []),
        ),
    )

    for field_source, values, provenance_ids in field_specs:
        for index, value in enumerate(values):
            field_text = _safe_text(value)
            if not field_text:
                continue
            field_tokens = set(_tokenise(field_text))
            if not _field_match(term, field_text=field_text, field_tokens=field_tokens):
                continue
            return {
                "paper_source": field_source,
                "matched_paper_text": field_text,
                "paper_reference_id": provenance_ids[index]
                if index < len(provenance_ids)
                else None,
            }
    return None


def _append_evidence(
    evidence_rows: list[dict[str, Any]],
    *,
    evidence_type: str,
    profile_source: str,
    profile_value: str,
    paper_source: str,
    matched_paper_text: str,
    weight: float,
    paper_reference_id: str | None = None,
    matched_tokens: Sequence[str] | None = None,
) -> None:
    evidence_rows.append(
        {
            "evidence_type": evidence_type,
            "profile_source": profile_source,
            "profile_value": profile_value,
            "paper_source": paper_source,
            "matched_paper_text": matched_paper_text,
            "matched_tokens": list(matched_tokens or []),
            "weight": round(weight, 4),
            "paper_reference_id": paper_reference_id,
        }
    )


def _score_candidate(
    *,
    profile_signals: Mapping[str, Any],
    paper: Mapping[str, Any],
) -> dict[str, Any]:
    evidence_rows: list[dict[str, Any]] = []
    rationale: list[str] = []
    score = 0.0
    positive_match_count = 0
    negative_match_count = 0

    matched_interest_terms: list[str] = []
    for term in profile_signals.get("stated_interest_terms") or []:
        match = _find_textual_match(str(term), paper=paper)
        if not match:
            continue
        positive_match_count += 1
        matched_interest_terms.append(str(term))
        score += 0.18
        _append_evidence(
            evidence_rows,
            evidence_type="interest_term_match",
            profile_source="profile.stated_interest_terms",
            profile_value=str(term),
            paper_source=str(match["paper_source"]),
            matched_paper_text=str(match["matched_paper_text"]),
            weight=0.18,
            paper_reference_id=(
                str(match["paper_reference_id"])
                if isinstance(match.get("paper_reference_id"), str)
                else None
            ),
        )

    matched_research_interests: list[str] = []
    for term in profile_signals.get("research_interest_names") or []:
        match = _find_textual_match(str(term), paper=paper)
        if not match:
            continue
        positive_match_count += 1
        matched_research_interests.append(str(term))
        score += 0.12
        _append_evidence(
            evidence_rows,
            evidence_type="research_interest_match",
            profile_source="profile.derived_context.research_interest_concepts",
            profile_value=str(term),
            paper_source=str(match["paper_source"]),
            matched_paper_text=str(match["matched_paper_text"]),
            weight=0.12,
            paper_reference_id=(
                str(match["paper_reference_id"])
                if isinstance(match.get("paper_reference_id"), str)
                else None
            ),
        )

    paper_author_names = [str(item) for item in (paper.get("author_names") or [])]
    matched_authors: list[str] = []
    author_concept_ids = list((paper.get("provenance") or {}).get("author_concept_ids") or [])
    for preferred_author in profile_signals.get("preferred_authors") or []:
        for index, author_name in enumerate(paper_author_names):
            if not _field_match(
                str(preferred_author),
                field_text=author_name,
                field_tokens=set(_tokenise(author_name)),
            ):
                continue
            positive_match_count += 1
            matched_authors.append(str(preferred_author))
            score += 0.2
            _append_evidence(
                evidence_rows,
                evidence_type="preferred_author_match",
                profile_source="profile.preferred_authors",
                profile_value=str(preferred_author),
                paper_source="paper.authors",
                matched_paper_text=author_name,
                weight=0.2,
                paper_reference_id=(
                    str(author_concept_ids[index])
                    if index < len(author_concept_ids)
                    and isinstance(author_concept_ids[index], str)
                    else None
                ),
            )
            break

    matched_venues: list[str] = []
    venue_relation_ids = list((paper.get("provenance") or {}).get("venue_relation_ids") or [])
    paper_venue_names = [str(item) for item in (paper.get("venue_names") or [])]
    for preferred_venue in profile_signals.get("preferred_venues") or []:
        for index, venue_name in enumerate(paper_venue_names):
            if not _field_match(
                str(preferred_venue),
                field_text=venue_name,
                field_tokens=set(_tokenise(venue_name)),
            ):
                continue
            positive_match_count += 1
            matched_venues.append(str(preferred_venue))
            score += 0.12
            _append_evidence(
                evidence_rows,
                evidence_type="preferred_venue_match",
                profile_source="profile.preferred_venues",
                profile_value=str(preferred_venue),
                paper_source="paper.venues",
                matched_paper_text=venue_name,
                weight=0.12,
                paper_reference_id=(
                    str(venue_relation_ids[index])
                    if index < len(venue_relation_ids)
                    and isinstance(venue_relation_ids[index], str)
                    else None
                ),
            )
            break

    paper_token_set = set(str(token) for token in (paper.get("token_set") or []))
    project_overlap = [
        token
        for token in (profile_signals.get("project_description_keywords") or [])
        if token in paper_token_set
    ]
    if project_overlap:
        positive_match_count += 1
        weight = min(0.16, 0.04 * len(project_overlap))
        score += weight
        matched_tokens = project_overlap[:4]
        _append_evidence(
            evidence_rows,
            evidence_type="project_description_overlap",
            profile_source="profile.project_description",
            profile_value=_safe_text((profile_signals.get("profile") or {}).get("project_description")),
            paper_source="paper.combined_text",
            matched_paper_text=", ".join(matched_tokens),
            matched_tokens=matched_tokens,
            weight=weight,
        )

    notes_overlap = [
        token
        for token in (profile_signals.get("notes_keywords") or [])
        if token in paper_token_set
    ]
    if notes_overlap:
        positive_match_count += 1
        weight = min(0.08, 0.03 * len(notes_overlap))
        score += weight
        matched_tokens = notes_overlap[:3]
        _append_evidence(
            evidence_rows,
            evidence_type="notes_overlap",
            profile_source="profile.notes",
            profile_value=_safe_text((profile_signals.get("profile") or {}).get("notes")),
            paper_source="paper.combined_text",
            matched_paper_text=", ".join(matched_tokens),
            matched_tokens=matched_tokens,
            weight=weight,
        )

    matched_negative_terms: list[str] = []
    for term in profile_signals.get("negative_interest_terms") or []:
        match = _find_textual_match(str(term), paper=paper)
        if not match:
            continue
        negative_match_count += 1
        matched_negative_terms.append(str(term))
        score -= 0.24
        _append_evidence(
            evidence_rows,
            evidence_type="negative_interest_match",
            profile_source="profile.negative_interest_terms",
            profile_value=str(term),
            paper_source=str(match["paper_source"]),
            matched_paper_text=str(match["matched_paper_text"]),
            weight=-0.24,
            paper_reference_id=(
                str(match["paper_reference_id"])
                if isinstance(match.get("paper_reference_id"), str)
                else None
            ),
        )

    score = max(0.0, min(1.0, score))

    if matched_interest_terms:
        rationale.append(
            "Matches stated interests: " + ", ".join(matched_interest_terms[:3]) + "."
        )
    if matched_research_interests:
        rationale.append(
            "Overlaps represented research interests: "
            + ", ".join(matched_research_interests[:3])
            + "."
        )
    if matched_authors:
        rationale.append(
            "Matches preferred authors: " + ", ".join(matched_authors[:2]) + "."
        )
    if matched_venues:
        rationale.append(
            "Matches preferred venues: " + ", ".join(matched_venues[:2]) + "."
        )
    if project_overlap:
        rationale.append(
            "Project-description overlap: " + ", ".join(project_overlap[:4]) + "."
        )
    if notes_overlap:
        rationale.append("Notes overlap: " + ", ".join(notes_overlap[:3]) + ".")
    if matched_negative_terms:
        rationale.append(
            "Contains negative-interest terms: "
            + ", ".join(matched_negative_terms[:3])
            + "."
        )

    if negative_match_count and score <= 0.1:
        recommendation_tier = "avoid"
    elif score >= 0.65:
        recommendation_tier = "strong"
    elif score >= 0.4:
        recommendation_tier = "moderate"
    elif score > 0:
        recommendation_tier = "weak"
    else:
        recommendation_tier = "unmatched"

    summary = rationale[0] if rationale else "No grounded recommendation evidence was found."

    return {
        "score": round(score, 4),
        "recommendation_tier": recommendation_tier,
        "rationale_summary": summary,
        "rationale": rationale[:6],
        "evidence": evidence_rows[:12],
        "positive_match_count": positive_match_count,
        "negative_match_count": negative_match_count,
    }


def _normalise_candidate_ids(candidate_paper_concept_ids: Sequence[str]) -> list[str]:
    candidate_ids: list[str] = []
    seen: set[str] = set()
    for item in candidate_paper_concept_ids:
        cleaned = _safe_text(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        candidate_ids.append(cleaned)
    return candidate_ids


def build_paper_recommendations(
    *,
    user_concept_id: str,
    candidate_paper_concept_ids: Sequence[str],
    max_results: int = 10,
    include_all_candidates: bool = False,
) -> dict[str, Any]:
    """Rank represented scholarly-paper candidates for one user's profile."""

    target_user = _safe_text(user_concept_id)
    if not target_user:
        return {
            "success": False,
            "error": "missing_user_concept_id",
            "message": "A target user_concept_id is required to load a recommendation profile.",
        }

    candidate_ids = _normalise_candidate_ids(candidate_paper_concept_ids)
    if not candidate_ids:
        return {
            "success": False,
            "error": "missing_candidate_paper_concept_ids",
            "message": "At least one candidate paper concept_id is required.",
            "details": {"missing": ["candidate_paper_concept_ids"]},
            "suggestions": [
                "Provide represented scholarly-paper concept IDs to rank",
                "Materialise scholarly-paper concepts before running recommendation",
            ],
        }

    profile_payload = load_paper_recommendation_profile(
        user_concept_id=target_user,
        create_if_missing=False,
    )
    if not isinstance(profile_payload, Mapping) or not profile_payload.get("success"):
        return {
            "success": False,
            "error": "profile_load_failed",
            "message": "Could not load the paper recommendation profile for the target user.",
            "user_concept_id": target_user,
            "profile_payload": dict(profile_payload)
            if isinstance(profile_payload, Mapping)
            else None,
        }

    profile_signals = _build_profile_signals(profile_payload=profile_payload)
    signal_summary = dict(profile_signals.get("signal_summary") or {})
    if not signal_summary.get("usable"):
        return {
            "success": False,
            "error": "insufficient_profile_signals",
            "message": (
                "The recommendation profile does not yet contain enough explicit signal "
                "to justify ranking candidate papers."
            ),
            "user_concept_id": target_user,
            "profile_concept_id": profile_signals.get("profile_concept_id"),
            "profile_signal_summary": signal_summary,
            "suggestions": [
                "Add stated interest terms or preferred authors/venues to the recommendation profile",
                "Link research-interest concepts to the target person concept",
                "Expand the project description with concrete topic language",
            ],
        }

    ranked_rows: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    for paper_concept_id in candidate_ids:
        paper = _build_paper_representation(paper_concept_id)
        if not paper.get("representation_complete"):
            skipped_rows.append(
                {
                    "paper_concept_id": paper_concept_id,
                    "paper_title": paper.get("paper_title") or paper_concept_id,
                    "status": "skipped",
                    "skip_reason": "insufficient_paper_representation",
                    "representation_failures": list(
                        paper.get("representation_failures") or []
                    ),
                    "paper_representation": {
                        "paper_title": paper.get("paper_title"),
                        "summary_present": bool(_safe_text(paper.get("paper_summary"))),
                        "author_names": list(paper.get("author_names") or []),
                        "topic_labels": list(paper.get("topic_labels") or []),
                        "venue_names": list(paper.get("venue_names") or []),
                        "publication_date": paper.get("publication_date"),
                    },
                    "provenance": dict(paper.get("provenance") or {}),
                }
            )
            continue

        scored = _score_candidate(profile_signals=profile_signals, paper=paper)
        ranked_rows.append(
            {
                "paper_concept_id": paper_concept_id,
                "paper_title": paper.get("paper_title") or paper_concept_id,
                "status": "ranked",
                "score": scored["score"],
                "recommendation_tier": scored["recommendation_tier"],
                "rationale_summary": scored["rationale_summary"],
                "rationale": scored["rationale"],
                "evidence": scored["evidence"],
                "paper_representation": {
                    "paper_title": paper.get("paper_title"),
                    "summary_excerpt": paper.get("summary_excerpt"),
                    "author_names": list(paper.get("author_names") or []),
                    "topic_labels": list(paper.get("topic_labels") or []),
                    "venue_names": list(paper.get("venue_names") or []),
                    "publication_date": paper.get("publication_date"),
                },
                "provenance": dict(paper.get("provenance") or {}),
                "positive_match_count": scored["positive_match_count"],
                "negative_match_count": scored["negative_match_count"],
            }
        )

    ranked_rows.sort(
        key=lambda item: (
            -float(item.get("score") or 0.0),
            str(item.get("paper_title") or ""),
            str(item.get("paper_concept_id") or ""),
        )
    )

    if include_all_candidates:
        results = ranked_rows + skipped_rows
    else:
        safe_max_results = max(1, min(int(max_results or 10), 100))
        results = ranked_rows[:safe_max_results] + skipped_rows

    if not ranked_rows:
        warnings.append(
            "No candidates could be ranked with grounded evidence; inspect skipped candidates and profile coverage."
        )

    if profile_signals.get("preferred_venues") and not any(
        row.get("paper_representation", {}).get("venue_names") for row in ranked_rows
    ):
        warnings.append(
            "Preferred venues are present in the profile, but none of the candidate papers expose venue metadata yet."
        )

    return {
        "success": True,
        "user_concept_id": target_user,
        "profile_concept_id": profile_signals.get("profile_concept_id"),
        "recommendation_policy_version": PAPER_RECOMMENDATION_POLICY_VERSION,
        "generated_at": _utc_now_iso(),
        "results": results,
        "ranked_count": len(ranked_rows),
        "skipped_count": len(skipped_rows),
        "candidate_count_requested": len(candidate_ids),
        "warning_count": len(warnings),
        "warnings": warnings,
        "profile_signal_summary": signal_summary,
        "profile_diagnostics": dict(profile_signals.get("diagnostics") or {}),
    }


__all__ = [
    "PAPER_RECOMMENDATION_POLICY_VERSION",
    "build_paper_recommendations",
]
