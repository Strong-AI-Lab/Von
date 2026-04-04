from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping

from . import concept_search_service
from .relationship_write_service import add_relationship
from .text_value_service import get_texts_for_concept, upsert_text_for_concept

_ARXIV_ID_PATTERN = re.compile(
    r"(?i)\b(?:arxiv:\s*|arxiv\.org/(?:abs|pdf)/)?"
    r"((?:[a-z\-]+/\d{7})|(?:\d{4}\.\d{4,5})(?:v\d+)?)\b"
)

_MAX_TOPIC_RELATIONS = 3
_PUBLICATION_DATE_PREDICATE_ID = "#V#has_publication_date"


def _normalise_arxiv_id(arxiv_id: str) -> str:
    value = str(arxiv_id or "").strip()
    if value.lower().startswith("arxiv:"):
        value = value.split(":", 1)[1].strip()
    return value


def _stable_paper_instance_concept_id(arxiv_id: str) -> str:
    raw = _normalise_arxiv_id(arxiv_id).lower()

    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

    # Keep IDs reasonably short for storage and UI.
    if len(slug) > 64:
        slug = slug[:64].rstrip("_")

    return (
        f"#V#paper_on_arxiv_{slug}_{digest}" if slug else f"#V#paper_on_arxiv_{digest}"
    )


def _stable_file_copy_paper_instance_concept_id(file_copy_concept_id: str) -> str:
    raw = str(file_copy_concept_id or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    if len(slug) > 64:
        slug = slug[:64].rstrip("_")
    return (
        f"#V#scholarly_paper_for_file_copy_{slug}_{digest}"
        if slug
        else f"#V#scholarly_paper_for_file_copy_{digest}"
    )


def _extract_string_candidates(raw_values: Iterable[Any]) -> list[str]:
    candidates: list[str] = []
    for value in raw_values:
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                candidates.append(cleaned)
            continue
        if isinstance(value, Mapping):
            nested = value.values()
            candidates.extend(_extract_string_candidates(nested))
            continue
        if isinstance(value, (list, tuple, set)):
            candidates.extend(_extract_string_candidates(value))
    return candidates


def extract_arxiv_id_candidates(*raw_values: Any) -> list[str]:
    """Extract unique arXiv IDs from free text values."""

    matches: list[str] = []
    seen: set[str] = set()
    for candidate in _extract_string_candidates(raw_values):
        for match in _ARXIV_ID_PATTERN.finditer(candidate):
            arxiv_id = _normalise_arxiv_id(match.group(1)).lower()
            if not arxiv_id or arxiv_id in seen:
                continue
            seen.add(arxiv_id)
            matches.append(arxiv_id)
    return matches


def _concept_exists(concept_id: str) -> bool:
    from . import concept_service

    try:
        return concept_service.get_concept_by_concept_id_exact(concept_id) is not None
    except Exception:
        return False


def _ensure_type_concept(
    concept_id: str,
    name: str,
    *,
    preferred_parent_id: str,
    logger: Any | None = None,
) -> None:
    from . import concept_service
    from ..vontology.utils_vontology import THING_PRIMARY_ID

    if _concept_exists(concept_id):
        return

    parent_id = preferred_parent_id if _concept_exists(preferred_parent_id) else THING_PRIMARY_ID
    try:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            parent_concept_ids=[parent_id],
            create_as_instance=False,
        )
    except Exception as exc:
        if logger is not None:
            logger.warning("[%s] Type ensure failed (continuing): %s", concept_id, exc)


def _ensure_name_text_relation(
    *,
    concept_id: str,
    text: str,
    source: str,
    logger: Any | None = None,
) -> None:
    cleaned_text = str(text or "").strip()
    if not concept_id or not cleaned_text:
        return
    try:
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasName",
            text=cleaned_text,
            lang="en-NZ",
            context={"name_type": "NL", "source": source},
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[arxiv_paper_representation] Name text ensure failed for %s: %s",
                concept_id,
                exc,
            )


def _ensure_predicate_concept(
    concept_id: str,
    name: str,
    *,
    logger: Any | None = None,
) -> None:
    from . import concept_service

    if _concept_exists(concept_id):
        return

    try:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            parent_concept_ids=["#V#predicate"],
            create_as_instance=True,
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[%s] Predicate ensure failed (continuing): %s",
                concept_id,
                exc,
            )


def _normalise_person_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned


def _extract_author_names(metadata: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(metadata, Mapping):
        return []

    raw = metadata.get("authors")
    candidates: list[str] = []
    if isinstance(raw, str):
        candidates.extend(re.split(r"[,\n;]+", raw))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, Mapping):
                for key in ("name", "full_name", "display_name", "author"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        candidates.append(value)
                        break

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        normalised = _normalise_person_name(item)
        if not normalised:
            continue
        fingerprint = normalised.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(normalised)
    return deduped


def _extract_topic_labels(metadata: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(metadata, Mapping):
        return []

    labels: list[str] = []
    for key in ("primary_category", "category", "topic", "field"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            labels.append(value.strip())

    categories = metadata.get("categories")
    if isinstance(categories, str):
        labels.extend(part.strip() for part in categories.split() if part.strip())
    elif isinstance(categories, list):
        for item in categories:
            if isinstance(item, str) and item.strip():
                labels.append(item.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for label in labels:
        fingerprint = label.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(label)
    return deduped


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
) -> str:
    from . import concept_service

    concept_id = predict_scholarly_author_concept_id(
        user_concept_id=user_concept_id,
        author_name=person_name,
    )
    if _concept_exists(concept_id):
        _ensure_name_text_relation(
            concept_id=concept_id,
            text=person_name,
            source="arxiv_author_metadata",
            logger=logger,
        )
        return concept_id
    try:
        concept_service.create_concept(
            name=person_name,
            concept_id=concept_id,
            parent_concept_ids=["#V#person"],
            create_as_instance=True,
            system_tags=["author", "scholarly", "arxiv"],
        )
        concept_service.update_concept(
            concept_id,
            {"relationships.specific_to_user": [user_concept_id.strip()]},
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[arxiv_paper_representation] Author concept create failed for %s: %s",
                person_name,
                exc,
            )
    if _concept_exists(concept_id):
        _ensure_name_text_relation(
            concept_id=concept_id,
            text=person_name,
            source="arxiv_author_metadata",
            logger=logger,
        )
    return concept_id


def _resolve_or_create_topic_concept_id(
    *,
    user_concept_id: str,
    topic_label: str,
    logger: Any | None = None,
) -> str:
    from . import concept_service

    concept_id = predict_scholarly_topic_concept_id(
        user_concept_id=user_concept_id,
        topic_label=topic_label,
    )
    if _concept_exists(concept_id):
        _ensure_name_text_relation(
            concept_id=concept_id,
            text=topic_label,
            source="arxiv_topic_metadata",
            logger=logger,
        )
        return concept_id
    try:
        concept_service.create_concept(
            name=topic_label,
            concept_id=concept_id,
            parent_concept_ids=["#V#research_topic"],
            create_as_instance=True,
            system_tags=["topic", "scholarly", "arxiv"],
        )
        concept_service.update_concept(
            concept_id,
            {"relationships.specific_to_user": [user_concept_id.strip()]},
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[arxiv_paper_representation] Topic concept create failed for %s: %s",
                topic_label,
                exc,
            )
    if _concept_exists(concept_id):
        _ensure_name_text_relation(
            concept_id=concept_id,
            text=topic_label,
            source="arxiv_topic_metadata",
            logger=logger,
        )
    return concept_id


def _extract_metadata_title(metadata: Mapping[str, Any] | None) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    for key in ("title", "paper_title", "name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_metadata_summary(metadata: Mapping[str, Any] | None) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    for key in ("summary", "abstract", "description"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_metadata_publication_date(
    metadata: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    for key in ("publication_date", "published", "submission_date", "date"):
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if "T" in text:
            text = text.split("T", 1)[0].strip()
        if re.match(r"^\d{4}/\d{2}/\d{2}$", text):
            text = text.replace("/", "-")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            return text
    return None


def extract_scholarly_author_names(
    metadata: Mapping[str, Any] | None,
) -> list[str]:
    """Return stable author-name candidates from scholarly metadata."""

    return _extract_author_names(metadata)


def extract_scholarly_topic_labels(
    metadata: Mapping[str, Any] | None,
) -> list[str]:
    """Return stable topic/category labels from scholarly metadata."""

    return _extract_topic_labels(metadata)


def extract_scholarly_metadata_title(
    metadata: Mapping[str, Any] | None,
) -> str | None:
    """Return the preferred title field from scholarly metadata."""

    return _extract_metadata_title(metadata)


def extract_scholarly_metadata_summary(
    metadata: Mapping[str, Any] | None,
) -> str | None:
    """Return the preferred summary/abstract field from scholarly metadata."""

    return _extract_metadata_summary(metadata)


def extract_scholarly_metadata_publication_date(
    metadata: Mapping[str, Any] | None,
) -> str | None:
    """Return the preferred publication/submission date from scholarly metadata."""

    return _extract_metadata_publication_date(metadata)


def resolve_or_create_scholarly_author_concept_id(
    *,
    user_concept_id: str,
    author_name: str,
    logger: Any | None = None,
) -> str:
    """Resolve or create a person concept for a scholarly author name."""

    return _resolve_or_create_person_concept_id(
        user_concept_id=user_concept_id,
        person_name=author_name,
        logger=logger,
    )


def predict_arxiv_paper_concept_id(*, arxiv_id: str) -> str:
    """Return the stable paper concept ID for an arXiv identifier."""

    return _stable_paper_instance_concept_id(arxiv_id)


def predict_scholarly_author_concept_id(
    *,
    user_concept_id: str,
    author_name: str,
) -> str:
    """Resolve the expected author concept ID without creating any concepts."""

    del user_concept_id
    search_result = concept_search_service.search_concepts(
        query=author_name,
        instance_of="#V#person",
        match_type="exact",
        limit=10,
    )
    for item in search_result.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        candidate_id = item.get("concept_id")
        candidate_name = item.get("name")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            continue
        if isinstance(candidate_name, str) and candidate_name.strip():
            if candidate_name.strip().casefold() == author_name.casefold():
                return candidate_id.strip()
    return _stable_named_instance_concept_id(author_name, prefix="person")


def predict_scholarly_topic_concept_id(
    *,
    user_concept_id: str,
    topic_label: str,
) -> str:
    """Resolve the expected topic concept ID without creating any concepts."""

    del user_concept_id
    search_result = concept_search_service.search_concepts(
        query=topic_label,
        instance_of="#V#research_topic",
        match_type="exact",
        limit=10,
    )
    for item in search_result.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        candidate_id = item.get("concept_id")
        candidate_name = item.get("name")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            continue
        if isinstance(candidate_name, str) and candidate_name.strip():
            if candidate_name.strip().casefold() == topic_label.casefold():
                return candidate_id.strip()
    return _stable_named_instance_concept_id(topic_label, prefix="research_topic")


def _relation_contains_target(
    concept_id: str,
    predicate: str,
    target_concept_id: str,
) -> bool:
    from . import concept_service

    try:
        concept_doc = concept_service.get_concept_by_concept_id_exact(concept_id) or {}
    except Exception:
        concept_doc = {}
    relationships = concept_doc.get("relationships") or {}
    raw_targets = relationships.get(predicate) or []
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    return target_concept_id in raw_targets


def ensure_paper_on_arxiv_type_exists(*, logger: Any | None = None) -> None:
    """Best-effort ensure the #V#paper_on_arxiv type exists."""

    type_concept_id = "#V#paper_on_arxiv"

    try:
        from . import concept_service
        from ..vontology.utils_vontology import (
            THING_PRIMARY_ID,
            ensure_thing_exists_and_link_orphans,
        )

        if _concept_exists(type_concept_id):
            return

        parent_id = (
            "#V#scholarly_work"
            if _concept_exists("#V#scholarly_work")
            else THING_PRIMARY_ID
        )
        if parent_id == THING_PRIMARY_ID:
            ensure_thing_exists_and_link_orphans()

        concept_service.create_concept(
            name="Paper on arXiv",
            concept_id=type_concept_id,
            parent_concept_ids=[parent_id],
            create_as_instance=False,
            description="A scholarly work available on arXiv.",
            notes="Created on-demand by Von's arXiv import flows.",
            system_tags=["arxiv", "paper"],
        )
    except Exception as exc:
        if logger is not None:
            logger.warning("[paper_on_arxiv] Type ensure failed (continuing): %s", exc)


def ensure_arxiv_paper_instance(
    *,
    user_concept_id: str,
    arxiv_id: str,
    logger: Any | None = None,
) -> str:
    """Ensure a stable per-arXiv-ID paper instance exists; returns its concept_id."""

    ensure_paper_on_arxiv_type_exists(logger=logger)

    from . import concept_service

    type_concept_id = "#V#paper_on_arxiv"
    normalised_arxiv_id = _normalise_arxiv_id(arxiv_id)
    instance_concept_id = predict_arxiv_paper_concept_id(arxiv_id=normalised_arxiv_id)

    existing = None
    try:
        existing = concept_service.get_concept_by_concept_id_exact(instance_concept_id)
    except Exception:
        existing = None

    if not existing:
        concept_service.create_concept(
            name=f"arXiv paper {normalised_arxiv_id}",
            concept_id=instance_concept_id,
            parent_concept_ids=[type_concept_id],
            create_as_instance=True,
            system_tags=["arxiv", "paper"],
            attributes={
                "source": "arxiv",
                "arxiv_id": normalised_arxiv_id,
            },
        )
        concept_service.update_concept(
            instance_concept_id,
            {"relationships.specific_to_user": [user_concept_id.strip()]},
        )

        # Make the arXiv identifier directly searchable without introducing a new predicate.
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="hasName",
            text=str(normalised_arxiv_id).strip(),
            lang="en-NZ",
            context={"name_type": "CODE"},
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="hasName",
            text=f"https://arxiv.org/abs/{str(normalised_arxiv_id).strip()}",
            lang="en-NZ",
            context={"name_type": "CODE"},
        )

    return instance_concept_id


def link_file_copy_to_arxiv_paper(
    *,
    user_concept_id: str,
    arxiv_id: str,
    file_copy_concept_id: str,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Link a #V#computer_file_copy to the corresponding #V#paper_on_arxiv instance."""

    paper_concept_id = ensure_arxiv_paper_instance(
        user_concept_id=user_concept_id,
        arxiv_id=arxiv_id,
        logger=logger,
    )
    changed = _link_file_copy_to_paper_concept(
        file_copy_concept_id=file_copy_concept_id,
        paper_concept_id=paper_concept_id,
    )

    return {
        "paper_concept_id": paper_concept_id,
        "linked": True,
        "changed": bool(changed),
    }


def _link_file_copy_to_paper_concept(
    *,
    file_copy_concept_id: str,
    paper_concept_id: str,
) -> bool:
    from ..db.repositories.concepts_repository import ConceptsRepository

    changed = ConceptsRepository.mutate_relationship_edge(
        file_copy_concept_id,
        "related_to",
        paper_concept_id,
        action="add",
        maintain_inverse=True,
    )
    changed = (
        ConceptsRepository.mutate_relationship_edge(
            file_copy_concept_id,
            "#V#computer_file_for_propositional_information_thing",
            paper_concept_id,
            action="add",
            maintain_inverse=False,
        )
        or changed
    )
    changed = (
        ConceptsRepository.mutate_relationship_edge(
            paper_concept_id,
            "#V#propositional_information_thing_has_computer_file",
            file_copy_concept_id,
            action="add",
            maintain_inverse=False,
        )
        or changed
    )
    return bool(changed)


def materialise_scholarly_representation_for_file_copy(
    *,
    user_concept_id: str,
    file_copy_concept_id: str,
    metadata: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Materialise a minimal scholarly-paper concept for a file-copy document."""

    from . import concept_service

    _ensure_type_concept(
        "#V#scholarly_article",
        "Scholarly Article",
        preferred_parent_id="#V#scholarly_work",
        logger=logger,
    )

    paper_concept_id = _stable_file_copy_paper_instance_concept_id(file_copy_concept_id)
    existing = None
    try:
        existing = concept_service.get_concept_by_concept_id_exact(paper_concept_id)
    except Exception:
        existing = None

    title = _extract_metadata_title(metadata)
    summary = _extract_metadata_summary(metadata)
    publication_date = _extract_metadata_publication_date(metadata)
    default_name = f"Scholarly paper for {file_copy_concept_id}"

    if not existing:
        concept_service.create_concept(
            name=title or default_name,
            concept_id=paper_concept_id,
            parent_concept_ids=["#V#scholarly_article"],
            create_as_instance=True,
            system_tags=["scholarly", "paper", "file_copy"],
            attributes={
                "source": "file_copy",
                "file_copy_concept_id": str(file_copy_concept_id).strip(),
            },
        )
        concept_service.update_concept(
            paper_concept_id,
            {"relationships.specific_to_user": [user_concept_id.strip()]},
        )

    add_relationship(
        source_id=paper_concept_id,
        predicate="is_an_instance_of",
        target="#V#scholarly_article",
    )
    _link_file_copy_to_paper_concept(
        file_copy_concept_id=file_copy_concept_id,
        paper_concept_id=paper_concept_id,
    )

    if isinstance(title, str) and title.strip():
        upsert_text_for_concept(
            subject_concept_id=paper_concept_id,
            predicate="hasName",
            text=title.strip(),
            lang="en-NZ",
            context={"name_type": "NL", "source": "file_copy_interpretation"},
        )

    if isinstance(summary, str) and summary.strip():
        upsert_text_for_concept(
            subject_concept_id=paper_concept_id,
            predicate="hasDescription",
            text=summary.strip(),
            lang="en-NZ",
            context={"source": "file_copy_interpretation"},
        )

    if isinstance(publication_date, str) and publication_date.strip():
        upsert_text_for_concept(
            subject_concept_id=paper_concept_id,
            predicate=_PUBLICATION_DATE_PREDICATE_ID,
            text=publication_date.strip(),
            lang="en-NZ",
            context={"source": "file_copy_interpretation"},
        )

    type_asserted = _relation_contains_target(
        paper_concept_id,
        "is_an_instance_of",
        "#V#scholarly_article",
    )
    file_link_verified = _relation_contains_target(
        paper_concept_id,
        "#V#propositional_information_thing_has_computer_file",
        file_copy_concept_id,
    )
    summary_present = bool(summary and summary.strip())
    title_present = bool(title and title.strip())
    publication_date_present = bool(publication_date and publication_date.strip())

    verification_failures: list[str] = []
    if not type_asserted:
        verification_failures.append("type_missing")
    if not file_link_verified:
        verification_failures.append("file_link_missing")

    result = {
        "success": len(verification_failures) == 0,
        "verified": len(verification_failures) == 0,
        "representation_mode": "generic_file_copy",
        "paper_concept_id": paper_concept_id,
        "file_copy_concept_id": file_copy_concept_id,
        "title": title,
        "title_present": title_present,
        "summary_present": summary_present,
        "publication_date": publication_date,
        "publication_date_present": publication_date_present,
        "type_asserted": type_asserted,
        "file_link_verified": file_link_verified,
        "verification_failures": verification_failures,
    }
    if result["success"]:
        try:
            from .paper_recommendation_workflow_vontology_service import (
                request_paper_recommendation_refresh,
            )

            result["recommendation_refresh"] = request_paper_recommendation_refresh(
                candidate_paper_concept_ids=[paper_concept_id],
                trigger_source="materialise_scholarly_representation_for_file_copy",
                user_id=user_concept_id,
                event_payload={
                    "paper_concept_id": paper_concept_id,
                    "file_copy_concept_id": file_copy_concept_id,
                    "source": "materialise_scholarly_representation_for_file_copy",
                },
            )
        except Exception as exc:
            result["recommendation_refresh"] = {
                "success": False,
                "triggered": False,
                "reason": f"refresh_request_failed:{exc}",
            }
    return result


def materialise_scholarly_representation_for_arxiv_file_copy(
    *,
    user_concept_id: str,
    arxiv_id: str,
    file_copy_concept_id: str,
    metadata: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Materialise a rich scholarly-paper representation for an uploaded arXiv file.

    This helper is intentionally deterministic and tool-free so workflow-driven
    upload pipelines can assert strong postconditions without relying on chat
    heuristics.
    """

    normalised_arxiv_id = _normalise_arxiv_id(arxiv_id)
    link_result = link_file_copy_to_arxiv_paper(
        user_concept_id=user_concept_id,
        arxiv_id=normalised_arxiv_id,
        file_copy_concept_id=file_copy_concept_id,
        logger=logger,
    )
    paper_concept_id = str(link_result.get("paper_concept_id") or "").strip()
    if not paper_concept_id:
        return {
            "success": False,
            "verified": False,
            "error": "missing_paper_concept_id",
            "arxiv_id": normalised_arxiv_id,
            "file_copy_concept_id": file_copy_concept_id,
        }

    _ensure_type_concept(
        "#V#scholarly_article",
        "Scholarly Article",
        preferred_parent_id="#V#paper_on_arxiv",
        logger=logger,
    )
    _ensure_type_concept(
        "#V#person",
        "Person",
        preferred_parent_id="#V#thing",
        logger=logger,
    )
    _ensure_type_concept(
        "#V#research_topic",
        "Research Topic",
        preferred_parent_id="#V#thing",
        logger=logger,
    )
    _ensure_predicate_concept("#V#authored_by", "Authored By", logger=logger)
    _ensure_predicate_concept("#V#about", "About", logger=logger)

    type_relation = add_relationship(
        source_id=paper_concept_id,
        predicate="is_an_instance_of",
        target="#V#scholarly_article",
    )

    title = _extract_metadata_title(metadata)
    summary = _extract_metadata_summary(metadata)
    publication_date = _extract_metadata_publication_date(metadata)
    author_names = _extract_author_names(metadata)
    topic_labels = _extract_topic_labels(metadata)

    title_asserted = False
    summary_asserted = False
    if isinstance(title, str) and title.strip():
        upsert_text_for_concept(
            subject_concept_id=paper_concept_id,
            predicate="hasName",
            text=title.strip(),
            lang="en-NZ",
            context={"name_type": "NL", "source": "arxiv_metadata"},
        )
        title_asserted = True

    if isinstance(summary, str) and summary.strip():
        upsert_text_for_concept(
            subject_concept_id=paper_concept_id,
            predicate="hasDescription",
            text=summary.strip(),
            lang="en-NZ",
            context={"source": "arxiv_metadata"},
        )
        summary_asserted = True

    publication_date_asserted = False
    if isinstance(publication_date, str) and publication_date.strip():
        upsert_text_for_concept(
            subject_concept_id=paper_concept_id,
            predicate=_PUBLICATION_DATE_PREDICATE_ID,
            text=publication_date.strip(),
            lang="en-NZ",
            context={"source": "arxiv_metadata"},
        )
        publication_date_asserted = True

    if topic_labels:
        upsert_text_for_concept(
            subject_concept_id=paper_concept_id,
            predicate="#V#has_topic_labels",
            text=", ".join(topic_labels),
            lang="en-NZ",
            context={"source": "arxiv_metadata"},
        )

    author_concept_ids: list[str] = []
    author_links_written = 0
    for author_name in author_names:
        author_concept_id = _resolve_or_create_person_concept_id(
            user_concept_id=user_concept_id,
            person_name=author_name,
            logger=logger,
        )
        author_concept_ids.append(author_concept_id)
        add_relationship(
            source_id=paper_concept_id,
            predicate="#V#authored_by",
            target=author_concept_id,
        )
        if not _relation_contains_target(
            paper_concept_id,
            "#V#authored_by",
            author_concept_id,
        ):
            from ..db.repositories.concepts_repository import ConceptsRepository

            ConceptsRepository.mutate_relationship_edge(
                paper_concept_id,
                "#V#authored_by",
                author_concept_id,
                action="add",
                maintain_inverse=False,
            )
        if _relation_contains_target(
            paper_concept_id,
            "#V#authored_by",
            author_concept_id,
        ):
            author_links_written += 1

    topic_concept_ids: list[str] = []
    topic_links_written = 0
    for label in topic_labels[:_MAX_TOPIC_RELATIONS]:
        topic_concept_id = _resolve_or_create_topic_concept_id(
            user_concept_id=user_concept_id,
            topic_label=label,
            logger=logger,
        )
        topic_concept_ids.append(topic_concept_id)
        add_relationship(
            source_id=paper_concept_id,
            predicate="#V#about",
            target=topic_concept_id,
        )
        if not _relation_contains_target(
            paper_concept_id,
            "#V#about",
            topic_concept_id,
        ):
            from ..db.repositories.concepts_repository import ConceptsRepository

            ConceptsRepository.mutate_relationship_edge(
                paper_concept_id,
                "#V#about",
                topic_concept_id,
                action="add",
                maintain_inverse=False,
            )
        if _relation_contains_target(
            paper_concept_id,
            "#V#about",
            topic_concept_id,
        ):
            topic_links_written += 1

    has_name_rows = get_texts_for_concept(
        subject_concept_id=paper_concept_id,
        predicate="hasName",
        limit=200,
    )
    paper_names = [
        str(row.get("text")).strip()
        for row in has_name_rows
        if isinstance(row, Mapping) and isinstance(row.get("text"), str)
    ]
    has_description_rows = get_texts_for_concept(
        subject_concept_id=paper_concept_id,
        predicate="hasDescription",
        limit=50,
    )

    id_asserted = normalised_arxiv_id.casefold() in {
        name.casefold() for name in paper_names
    }
    file_link_verified = _relation_contains_target(
        paper_concept_id,
        "#V#propositional_information_thing_has_computer_file",
        file_copy_concept_id,
    )
    type_asserted = bool(type_relation.get("success")) and _relation_contains_target(
        paper_concept_id,
        "is_an_instance_of",
        "#V#scholarly_article",
    )
    author_asserted = bool(author_concept_ids) and author_links_written >= len(
        author_concept_ids
    )
    topic_asserted = bool(topic_labels) and topic_links_written > 0
    summary_present = bool(has_description_rows) or summary_asserted

    verification_failures: list[str] = []
    if not id_asserted:
        verification_failures.append("arxiv_identifier_missing")
    if not title_asserted:
        verification_failures.append("title_missing")
    if not summary_present:
        verification_failures.append("summary_missing")
    if not publication_date_asserted:
        verification_failures.append("publication_date_missing")
    if not author_asserted:
        verification_failures.append("authors_missing")
    if not type_asserted:
        verification_failures.append("type_missing")
    if not topic_asserted:
        verification_failures.append("topic_missing")
    if not file_link_verified:
        verification_failures.append("file_link_missing")

    result = {
        "success": len(verification_failures) == 0,
        "verified": len(verification_failures) == 0,
        "arxiv_id": normalised_arxiv_id,
        "paper_concept_id": paper_concept_id,
        "file_copy_concept_id": file_copy_concept_id,
        "title": title,
        "author_names": author_names,
        "author_concept_ids": author_concept_ids,
        "author_links_written": author_links_written,
        "topic_labels": topic_labels,
        "topic_concept_ids": topic_concept_ids,
        "topic_links_written": topic_links_written,
        "summary_present": summary_present,
        "publication_date": publication_date,
        "publication_date_asserted": publication_date_asserted,
        "type_asserted": type_asserted,
        "file_link_verified": file_link_verified,
        "verification_failures": verification_failures,
    }
    if result["success"]:
        try:
            from .paper_recommendation_workflow_vontology_service import (
                request_paper_recommendation_refresh,
            )

            result["recommendation_refresh"] = request_paper_recommendation_refresh(
                candidate_paper_concept_ids=[paper_concept_id],
                trigger_source="materialise_scholarly_representation_for_arxiv_file_copy",
                user_id=user_concept_id,
                event_payload={
                    "paper_concept_id": paper_concept_id,
                    "arxiv_id": normalised_arxiv_id,
                    "file_copy_concept_id": file_copy_concept_id,
                    "source": "materialise_scholarly_representation_for_arxiv_file_copy",
                },
            )
        except Exception as exc:
            result["recommendation_refresh"] = {
                "success": False,
                "triggered": False,
                "reason": f"refresh_request_failed:{exc}",
            }
    return result


__all__ = [
    "ensure_arxiv_paper_instance",
    "ensure_paper_on_arxiv_type_exists",
    "extract_arxiv_id_candidates",
    "extract_scholarly_author_names",
    "extract_scholarly_metadata_publication_date",
    "extract_scholarly_metadata_summary",
    "extract_scholarly_metadata_title",
    "extract_scholarly_topic_labels",
    "link_file_copy_to_arxiv_paper",
    "materialise_scholarly_representation_for_file_copy",
    "materialise_scholarly_representation_for_arxiv_file_copy",
    "predict_arxiv_paper_concept_id",
    "predict_scholarly_author_concept_id",
    "predict_scholarly_topic_concept_id",
    "resolve_or_create_scholarly_author_concept_id",
]
