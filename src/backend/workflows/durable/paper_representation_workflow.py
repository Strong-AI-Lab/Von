"""Reusable durable actions for scholarly-paper and arXiv workflows."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ...services import concept_service
from ...services.arxiv_paper_link_service import (
    extract_arxiv_id_candidates,
    extract_scholarly_author_names,
    extract_scholarly_metadata_publication_date,
    extract_scholarly_metadata_summary,
    extract_scholarly_metadata_title,
    extract_scholarly_topic_labels,
    materialise_scholarly_representation_for_arxiv_file_copy,
    materialise_scholarly_representation_for_file_copy,
    resolve_or_create_scholarly_author_concept_id,
)
from ...services.relationship_write_service import add_relationship
from ...services.text_value_service import get_texts_for_concept, upsert_text_for_concept
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..execution_contracts import build_arxiv_ingestion_completion_report

logger = logging.getLogger(__name__)
_PUBLICATION_DATE_PREDICATE_ID = "#V#has_publication_date"

SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID = "scholarly_paper.normalise_inputs"
SCHOLARLY_PAPER_ENSURE_PAPER_CONCEPT_ACTION_ID = "scholarly_paper.ensure_paper_concept"
SCHOLARLY_PAPER_LINK_FILE_COPY_ACTION_ID = "scholarly_paper.link_file_copy"
SCHOLARLY_PAPER_ATTACH_METADATA_ACTION_ID = "scholarly_paper.attach_metadata"
SCHOLARLY_PAPER_MATERIALISE_ACTION_ID = "scholarly_paper.materialise_from_file_copy"
SCHOLARLY_PAPER_ENRICH_ACTION_ID = "scholarly_paper.enrich_from_metadata"
SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID = "scholarly_paper.resolve_authors"
SCHOLARLY_PAPER_RESOLVE_TOPICS_ACTION_ID = "scholarly_paper.resolve_topics"
SCHOLARLY_PAPER_ASSERT_SOURCE_IDENTITY_ACTION_ID = "scholarly_paper.assert_source_identity"
SCHOLARLY_PAPER_VERIFY_ACTION_ID = "scholarly_paper.verify_representation"

ARXIV_NORMALISE_SOURCE_ACTION_ID = "arxiv.normalise_source"
ARXIV_INSPECT_EXISTING_STATE_ACTION_ID = "arxiv.inspect_existing_state"
ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID = "arxiv.decide_acquisition_mode"
ARXIV_BUILD_COMPLETION_REPORT_ACTION_ID = "arxiv.build_completion_report"

ARXIV_ACQUISITION_MODE_EXISTING_FILE_COPY = "existing_file_copy"
ARXIV_ACQUISITION_MODE_FINALISE_CACHED_PDF = "finalise_cached_pdf"
ARXIV_ACQUISITION_MODE_REACQUIRE_PARTIAL_CACHE = "reacquire_partial_cache"
ARXIV_ACQUISITION_MODE_DOWNLOAD_FROM_SOURCE = "download_from_source"


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _coerce_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []

    values: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item)
        if not text:
            continue
        lowered = text.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        values.append(text)
    return values


def _relationship_targets(concept_doc: Mapping[str, Any] | None, predicate: str) -> list[str]:
    relationships = (
        concept_doc.get("relationships")
        if isinstance(concept_doc, Mapping)
        else None
    )
    if not isinstance(relationships, Mapping):
        return []
    raw = relationships.get(predicate)
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if _clean_text(item)]
    return []


def _relation_contains_target(
    concept_doc: Mapping[str, Any] | None,
    predicate: str,
    target: str,
) -> bool:
    target_clean = _clean_text(target)
    return bool(target_clean) and target_clean in _relationship_targets(concept_doc, predicate)


def _get_concept(concept_id: str) -> dict[str, Any] | None:
    concept_id_clean = _clean_text(concept_id)
    if not concept_id_clean:
        return None
    try:
        concept = concept_service.get_concept_by_concept_id_exact(concept_id_clean)
    except Exception:
        return None
    return concept if isinstance(concept, Mapping) else None


def _first_non_empty_text(*values: Any) -> str | None:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return None


def _resolve_user_concept_id(request: WorkflowActionRequest) -> str | None:
    from ...services.workflow_event_integration_service import (
        resolve_event_actor_context,
    )

    explicit_user = _first_non_empty_text(
        request.inputs.get("user_concept_id"),
        request.data.get("user_concept_id"),
    )
    resolved_user, _resolved_org = resolve_event_actor_context(
        user_id=explicit_user,
        namespace=_clean_text(getattr(request.environment, "user_namespace", None)) or None,
    )
    return _clean_text(resolved_user) or None


def _extract_metadata_from_context(request: WorkflowActionRequest) -> dict[str, Any]:
    for source in (
        request.inputs.get("paper_metadata"),
        request.inputs.get("metadata"),
        request.inputs.get("scholarly_metadata"),
        request.data.get("paper_metadata"),
        request.data.get("metadata"),
        request.data.get("scholarly_metadata"),
    ):
        if isinstance(source, Mapping):
            return dict(source)

    scholarly_representation = request.data.get("scholarly_representation")
    if isinstance(scholarly_representation, Mapping):
        derived: dict[str, Any] = {}
        title = _clean_text(scholarly_representation.get("title"))
        if title:
            derived["title"] = title
        author_names = _coerce_string_list(scholarly_representation.get("author_names"))
        if author_names:
            derived["authors"] = author_names
        topic_labels = _coerce_string_list(scholarly_representation.get("topic_labels"))
        if topic_labels:
            derived["categories"] = topic_labels
        publication_date = _clean_text(scholarly_representation.get("publication_date"))
        if publication_date:
            derived["publication_date"] = publication_date
        if derived:
            return derived

    derived: dict[str, Any] = {}
    title = _first_non_empty_text(
        request.inputs.get("title"),
        request.data.get("title"),
        request.inputs.get("paper_title"),
        request.data.get("paper_title"),
    )
    summary = _first_non_empty_text(
        request.inputs.get("summary"),
        request.data.get("summary"),
        request.inputs.get("abstract"),
        request.data.get("abstract"),
    )
    author_names = _coerce_string_list(
        request.inputs.get("author_names") or request.data.get("author_names")
    )
    topic_labels = _coerce_string_list(
        request.inputs.get("topic_labels")
        or request.data.get("topic_labels")
        or request.inputs.get("categories")
        or request.data.get("categories")
    )
    if title:
        derived["title"] = title
    if summary:
        derived["summary"] = summary
    publication_date = _first_non_empty_text(
        request.inputs.get("publication_date"),
        request.data.get("publication_date"),
    )
    if publication_date:
        derived["publication_date"] = publication_date
    if author_names:
        derived["authors"] = author_names
    if topic_labels:
        derived["categories"] = topic_labels
    return derived


def _extract_author_names_from_request(request: WorkflowActionRequest) -> list[str]:
    for raw in (
        request.inputs.get("author_names"),
        request.inputs.get("authors"),
        request.data.get("author_names"),
        request.data.get("authors"),
    ):
        values = _coerce_string_list(raw)
        if values:
            return values
    return extract_scholarly_author_names(_extract_metadata_from_context(request))


def _extract_topic_labels_from_request(request: WorkflowActionRequest) -> list[str]:
    for raw in (
        request.inputs.get("topic_labels"),
        request.inputs.get("categories"),
        request.data.get("topic_labels"),
        request.data.get("categories"),
    ):
        values = _coerce_string_list(raw)
        if values:
            return values
    return extract_scholarly_topic_labels(_extract_metadata_from_context(request))


def _extract_title_from_request(request: WorkflowActionRequest) -> str | None:
    metadata = _extract_metadata_from_context(request)
    return _first_non_empty_text(
        request.inputs.get("title"),
        request.data.get("title"),
        extract_scholarly_metadata_title(metadata),
    )


def _extract_summary_from_request(request: WorkflowActionRequest) -> str | None:
    metadata = _extract_metadata_from_context(request)
    return _first_non_empty_text(
        request.inputs.get("summary"),
        request.data.get("summary"),
        request.inputs.get("abstract"),
        request.data.get("abstract"),
        extract_scholarly_metadata_summary(metadata),
    )


def _extract_publication_date_from_request(
    request: WorkflowActionRequest,
) -> str | None:
    metadata = _extract_metadata_from_context(request)
    return _first_non_empty_text(
        request.inputs.get("publication_date"),
        request.data.get("publication_date"),
        extract_scholarly_metadata_publication_date(metadata),
    )


def _extract_arxiv_id_from_request(request: WorkflowActionRequest) -> str | None:
    candidates = extract_arxiv_id_candidates(
        request.inputs.get("arxiv_id"),
        request.data.get("arxiv_id"),
        request.inputs.get("source_uri"),
        request.data.get("source_uri"),
        request.inputs.get("prompt"),
        request.data.get("prompt"),
        request.inputs.get("original_filename"),
        request.data.get("original_filename"),
    )
    if candidates:
        return candidates[0]
    return _first_non_empty_text(
        request.inputs.get("arxiv_id"),
        request.data.get("arxiv_id"),
    )


def _resolve_verification_profile(request: WorkflowActionRequest) -> str:
    profile = _clean_text(
        request.inputs.get("verification_profile")
        or request.data.get("verification_profile")
    ).lower()
    if profile in {"generic", "arxiv"}:
        return profile
    return "arxiv" if _extract_arxiv_id_from_request(request) else "generic"


def _build_normalise_inputs_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        file_copy_concept_id = _first_non_empty_text(
            request.inputs.get("file_copy_concept_id"),
            request.inputs.get("concept_id"),
            request.data.get("file_copy_concept_id"),
            request.data.get("concept_id"),
        )
        if not file_copy_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_file_copy_concept_id_missing",
            )

        metadata = _extract_metadata_from_context(request)
        outputs = {
            "file_copy_concept_id": file_copy_concept_id,
            "paper_concept_id": _first_non_empty_text(
                request.inputs.get("paper_concept_id"),
                request.data.get("paper_concept_id"),
            ),
            "paper_metadata": metadata or None,
            "source_uri": _first_non_empty_text(
                request.inputs.get("source_uri"),
                request.data.get("source_uri"),
            ),
            "source_label": _first_non_empty_text(
                request.inputs.get("source_label"),
                request.data.get("source_label"),
            ),
            "arxiv_id": _extract_arxiv_id_from_request(request),
            "verification_profile": _resolve_verification_profile(request),
            "author_names": _extract_author_names_from_request(request),
            "topic_labels": _extract_topic_labels_from_request(request),
            "title": _extract_title_from_request(request),
            "summary": _extract_summary_from_request(request),
            "publication_date": _extract_publication_date_from_request(request),
            "normalised_scholarly_inputs": True,
        }
        return WorkflowActionResult(status="success", outputs=outputs)

    return _handle


def _build_materialise_from_file_copy_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        user_concept_id = _resolve_user_concept_id(request)
        if not user_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_actor_user_missing",
            )

        file_copy_concept_id = _first_non_empty_text(
            request.inputs.get("file_copy_concept_id"),
            request.data.get("file_copy_concept_id"),
        )
        if not file_copy_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_file_copy_concept_id_missing",
            )

        arxiv_id = _extract_arxiv_id_from_request(request)
        metadata = _extract_metadata_from_context(request)
        if arxiv_id:
            report = materialise_scholarly_representation_for_arxiv_file_copy(
                user_concept_id=user_concept_id,
                arxiv_id=arxiv_id,
                file_copy_concept_id=file_copy_concept_id,
                metadata=metadata or None,
                logger=logger,
            )
        else:
            report = materialise_scholarly_representation_for_file_copy(
                user_concept_id=user_concept_id,
                file_copy_concept_id=file_copy_concept_id,
                metadata=metadata or None,
                logger=logger,
            )

        if not isinstance(report, Mapping):
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_materialisation_invalid_response",
            )

        paper_concept_id = _clean_text(report.get("paper_concept_id"))
        if not paper_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_materialisation_missing_paper_concept_id",
            )

        outputs = {
            "paper_concept_id": paper_concept_id,
            "file_copy_concept_id": file_copy_concept_id,
            "arxiv_id": arxiv_id,
            "publication_date": _first_non_empty_text(
                report.get("publication_date"),
                request.inputs.get("publication_date"),
                request.data.get("publication_date"),
            ),
            "scholarly_representation": dict(report),
            "representation_mode": _first_non_empty_text(
                report.get("representation_mode"),
                "arxiv_file_copy" if arxiv_id else "generic_file_copy",
            ),
            "scholarly_representation_materialised": True,
        }
        return WorkflowActionResult(status="success", outputs=outputs)

    return _handle


def _build_enrich_from_metadata_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        paper_concept_id = _first_non_empty_text(
            request.inputs.get("paper_concept_id"),
            request.data.get("paper_concept_id"),
        )
        if not paper_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_paper_concept_id_missing",
            )

        title = _extract_title_from_request(request)
        summary = _extract_summary_from_request(request)
        topic_labels = _extract_topic_labels_from_request(request)
        publication_date = _extract_publication_date_from_request(request)
        arxiv_id = _extract_arxiv_id_from_request(request)
        source_uri = _first_non_empty_text(
            request.inputs.get("source_uri"),
            request.data.get("source_uri"),
        )

        applied = False
        if title:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                text=title,
                lang="en-NZ",
                context={"name_type": "NL", "source": "scholarly_workflow_enrichment"},
            )
            applied = True

        if summary:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasDescription",
                text=summary,
                lang="en-NZ",
                context={"source": "scholarly_workflow_enrichment"},
            )
            applied = True

        if topic_labels:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="#V#has_topic_labels",
                text=", ".join(topic_labels),
                lang="en-NZ",
                context={"source": "scholarly_workflow_enrichment"},
            )
            applied = True

        if publication_date:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate=_PUBLICATION_DATE_PREDICATE_ID,
                text=publication_date,
                lang="en-NZ",
                context={"source": "scholarly_workflow_enrichment"},
            )
            applied = True

        if arxiv_id:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                text=arxiv_id,
                lang="en-NZ",
                context={"name_type": "CODE", "source": "scholarly_workflow_enrichment"},
            )
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                text=f"https://arxiv.org/abs/{arxiv_id}",
                lang="en-NZ",
                context={"name_type": "CODE", "source": "scholarly_workflow_enrichment"},
            )
            applied = True

        if source_uri:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                text=source_uri,
                lang="en-NZ",
                context={"name_type": "CODE", "source": "scholarly_workflow_enrichment"},
            )
            applied = True

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "title": title,
                "summary": summary,
                "topic_labels": topic_labels,
                "publication_date": publication_date,
                "metadata_enrichment_applied": applied,
            },
        )

    return _handle


def _build_resolve_authors_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        paper_concept_id = _first_non_empty_text(
            request.inputs.get("paper_concept_id"),
            request.data.get("paper_concept_id"),
        )
        if not paper_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_paper_concept_id_missing",
            )

        paper_doc = _get_concept(paper_concept_id)
        existing_author_concept_ids = _relationship_targets(paper_doc, "#V#authored_by")
        author_names = _extract_author_names_from_request(request)
        if not author_names:
            return WorkflowActionResult(
                status="success",
                outputs={
                    "paper_concept_id": paper_concept_id,
                    "author_names": [],
                    "author_concept_ids": existing_author_concept_ids,
                    "author_resolution_completed": True,
                },
            )

        user_concept_id = _resolve_user_concept_id(request)
        if not user_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_author_actor_user_missing",
            )

        resolved_author_concept_ids: list[str] = []
        for author_name in author_names:
            author_concept_id = resolve_or_create_scholarly_author_concept_id(
                user_concept_id=user_concept_id,
                author_name=author_name,
                logger=logger,
            )
            resolved_author_concept_ids.append(author_concept_id)
            add_relationship(
                source_id=paper_concept_id,
                predicate="#V#authored_by",
                target=author_concept_id,
            )

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "author_names": author_names,
                "author_concept_ids": list(
                    dict.fromkeys([*existing_author_concept_ids, *resolved_author_concept_ids])
                ),
                "author_resolution_completed": True,
            },
        )

    return _handle


def _has_non_code_title(name_rows: list[Mapping[str, Any]], *, arxiv_id: str | None) -> bool:
    arxiv_id_clean = _clean_text(arxiv_id).casefold()
    disallowed: set[str] = set()
    if arxiv_id_clean:
        disallowed.add(arxiv_id_clean)
        disallowed.add(f"https://arxiv.org/abs/{arxiv_id_clean}")
    for row in name_rows:
        text = _clean_text(row.get("text")).casefold()
        if not text:
            continue
        if text in disallowed:
            continue
        return True
    return False


def _build_verify_representation_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        paper_concept_id = _first_non_empty_text(
            request.inputs.get("paper_concept_id"),
            request.data.get("paper_concept_id"),
        )
        file_copy_concept_id = _first_non_empty_text(
            request.inputs.get("file_copy_concept_id"),
            request.data.get("file_copy_concept_id"),
        )
        verification_profile = _resolve_verification_profile(request)
        arxiv_id = _extract_arxiv_id_from_request(request)
        expected_publication_date = _extract_publication_date_from_request(request)

        verification_failures: list[str] = []
        paper_doc = _get_concept(paper_concept_id or "")
        if not paper_doc:
            verification_failures.append("paper_concept_missing")

        if not file_copy_concept_id:
            verification_failures.append("file_copy_concept_missing")

        name_rows = (
            get_texts_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                limit=200,
            )
            if paper_concept_id
            else []
        )
        description_rows = (
            get_texts_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasDescription",
                limit=50,
            )
            if paper_concept_id
            else []
        )
        publication_date_rows = (
            get_texts_for_concept(
                subject_concept_id=paper_concept_id,
                predicate=_PUBLICATION_DATE_PREDICATE_ID,
                limit=10,
            )
            if paper_concept_id
            else []
        )

        type_asserted = _relation_contains_target(
            paper_doc,
            "is_an_instance_of",
            "#V#scholarly_article",
        )
        if not type_asserted:
            verification_failures.append("type_missing")

        file_link_verified = False
        if file_copy_concept_id:
            file_link_verified = _relation_contains_target(
                paper_doc,
                "#V#propositional_information_thing_has_computer_file",
                file_copy_concept_id,
            )
        if not file_link_verified:
            verification_failures.append("file_link_missing")

        has_name_signal = bool(name_rows) or bool(_clean_text((paper_doc or {}).get("name")))
        if not has_name_signal:
            verification_failures.append("title_or_name_missing")

        author_concept_ids = _relationship_targets(paper_doc, "#V#authored_by")
        topic_concept_ids = _relationship_targets(paper_doc, "#V#about")
        topic_label_rows = (
            get_texts_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="#V#has_topic_labels",
                limit=20,
            )
            if paper_concept_id
            else []
        )

        if verification_profile == "arxiv":
            paper_attributes = (
                paper_doc.get("attributes")
                if isinstance(paper_doc, Mapping)
                else None
            )
            stored_arxiv_id = _clean_text(
                (paper_attributes or {}).get("arxiv_id")
                if isinstance(paper_attributes, Mapping)
                else None
            )
            id_asserted = bool(_clean_text(arxiv_id)) and (
                _clean_text(arxiv_id).casefold() == stored_arxiv_id.casefold()
                or any(
                    _clean_text(row.get("text")).casefold()
                    in {
                        _clean_text(arxiv_id).casefold(),
                        f"https://arxiv.org/abs/{_clean_text(arxiv_id).casefold()}",
                    }
                    for row in name_rows
                    if isinstance(row, Mapping)
                )
            )
            if not id_asserted:
                verification_failures.append("arxiv_identifier_missing")

            if not _has_non_code_title(
                [row for row in name_rows if isinstance(row, Mapping)],
                arxiv_id=arxiv_id,
            ):
                verification_failures.append("title_missing")

            if not description_rows:
                verification_failures.append("summary_missing")

            represented_publication_dates = [
                _clean_text(row.get("text"))
                for row in publication_date_rows
                if isinstance(row, Mapping)
            ]
            represented_publication_dates = [
                item for item in represented_publication_dates if item
            ]
            if not represented_publication_dates:
                verification_failures.append("publication_date_missing")
            elif expected_publication_date and expected_publication_date not in (
                represented_publication_dates
            ):
                verification_failures.append("publication_date_mismatch")

            if not author_concept_ids:
                verification_failures.append("authors_missing")

            topic_labels_requested = bool(_extract_topic_labels_from_request(request))
            topic_evidence_present = bool(topic_concept_ids or topic_label_rows)
            if topic_labels_requested and not topic_evidence_present:
                verification_failures.append("topic_missing")

        verified = not verification_failures
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": verified,
                "paper_concept_id": paper_concept_id,
                "file_copy_concept_id": file_copy_concept_id,
                "verification_profile": verification_profile,
                "scholarly_representation_verified": verified,
                "verification_failures": verification_failures,
                "author_concept_ids": author_concept_ids,
                "topic_concept_ids": topic_concept_ids,
                "publication_date": (
                    _clean_text(publication_date_rows[0].get("text"))
                    if publication_date_rows and isinstance(publication_date_rows[0], Mapping)
                    else None
                ),
                "type_asserted": type_asserted,
                "file_link_verified": file_link_verified,
            },
        )

    return _handle


def _build_scholarly_paper_ensure_paper_concept_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        from ...services.arxiv_paper_link_service import (
            _ensure_type_concept,
            _stable_file_copy_paper_instance_concept_id,
            ensure_arxiv_paper_instance,
        )

        user_concept_id = _resolve_user_concept_id(request)
        if not user_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_actor_user_missing",
            )

        arxiv_id = _extract_arxiv_id_from_request(request)
        file_copy_concept_id = _first_non_empty_text(
            request.inputs.get("file_copy_concept_id"),
            request.data.get("file_copy_concept_id"),
        )

        # 1. Ensure the scholarly article type exists
        _ensure_type_concept(
            "#V#scholarly_article",
            "Scholarly Article",
            preferred_parent_id="#V#scholarly_work",
            logger=logger,
        )

        # 2. Determine or create the paper instance
        if arxiv_id:
            paper_concept_id = ensure_arxiv_paper_instance(
                user_concept_id=user_concept_id,
                arxiv_id=arxiv_id,
                logger=logger,
            )
            created = not _concept_exists(paper_concept_id) # ensure_arxiv_paper_instance might have created it
        elif file_copy_concept_id:
            paper_concept_id = _stable_file_copy_paper_instance_concept_id(file_copy_concept_id)
            created = False
            if not _concept_exists(paper_concept_id):
                metadata = _extract_metadata_from_context(request)
                title = _extract_metadata_title(metadata)
                default_name = f"Scholarly paper for {file_copy_concept_id}"
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
                created = True
        else:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_insufficient_identifiers",
            )

        # 3. Ensure it is explicitly typed as a scholarly article
        add_relationship(
            source_id=paper_concept_id,
            predicate="is_an_instance_of",
            target="#V#scholarly_article",
        )

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "paper_concept_created": created,
                "type_asserted": True,
            },
        )

    return _handle


def _build_scholarly_paper_link_file_copy_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        from ...services.arxiv_paper_link_service import _link_file_copy_to_paper_concept

        paper_concept_id = _first_non_empty_text(
            request.inputs.get("paper_concept_id"),
            request.data.get("paper_concept_id"),
        )
        file_copy_concept_id = _first_non_empty_text(
            request.inputs.get("file_copy_concept_id"),
            request.data.get("file_copy_concept_id"),
        )

        if not paper_concept_id or not file_copy_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_link_missing_ids",
            )

        changed = _link_file_copy_to_paper_concept(
            file_copy_concept_id=file_copy_concept_id,
            paper_concept_id=paper_concept_id,
        )

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "file_copy_concept_id": file_copy_concept_id,
                "file_link_written": True,
                "file_link_changed": bool(changed),
            },
        )

    return _handle


def _build_scholarly_paper_attach_metadata_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        paper_concept_id = _first_non_empty_text(
            request.inputs.get("paper_concept_id"),
            request.data.get("paper_concept_id"),
        )
        if not paper_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_paper_concept_id_missing",
            )

        title = _extract_title_from_request(request)
        summary = _extract_summary_from_request(request)
        publication_date = _extract_publication_date_from_request(request)

        applied = False
        if title:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                text=title,
                lang="en-NZ",
                context={"name_type": "NL", "source": "scholarly_metadata_attach"},
            )
            applied = True

        if summary:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasDescription",
                text=summary,
                lang="en-NZ",
                context={"source": "scholarly_metadata_attach"},
            )
            applied = True

        if publication_date:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate=_PUBLICATION_DATE_PREDICATE_ID,
                text=publication_date,
                lang="en-NZ",
                context={"source": "scholarly_metadata_attach"},
            )
            applied = True

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "title_attached": bool(title),
                "summary_attached": bool(summary),
                "publication_date_attached": bool(publication_date),
                "metadata_attached": applied,
            },
        )

    return _handle


def _build_scholarly_paper_resolve_topics_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        from ...services.arxiv_paper_link_service import (
            _resolve_or_create_topic_concept_id,
            _ensure_type_concept,
            _ensure_predicate_concept,
        )

        paper_concept_id = _first_non_empty_text(
            request.inputs.get("paper_concept_id"),
            request.data.get("paper_concept_id"),
        )
        if not paper_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_paper_concept_id_missing",
            )

        topic_labels = _extract_topic_labels_from_request(request)
        if not topic_labels:
            return WorkflowActionResult(
                status="success",
                outputs={
                    "paper_concept_id": paper_concept_id,
                    "topic_concept_ids": [],
                    "topic_resolution_completed": True,
                },
            )

        user_concept_id = _resolve_user_concept_id(request)
        if not user_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_topic_actor_user_missing",
            )

        _ensure_type_concept(
            "#V#research_topic",
            "Research Topic",
            preferred_parent_id="#V#thing",
            logger=logger,
        )
        _ensure_predicate_concept("#V#about", "About", logger=logger)

        topic_concept_ids: list[str] = []
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

        if topic_labels:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="#V#has_topic_labels",
                text=", ".join(topic_labels),
                lang="en-NZ",
                context={"source": "scholarly_topic_attach"},
            )

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "topic_labels": topic_labels,
                "topic_concept_ids": topic_concept_ids,
                "topic_resolution_completed": True,
            },
        )

    return _handle


def _build_scholarly_paper_assert_source_identity_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        paper_concept_id = _first_non_empty_text(
            request.inputs.get("paper_concept_id"),
            request.data.get("paper_concept_id"),
        )
        if not paper_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_paper_concept_id_missing",
            )

        arxiv_id = _extract_arxiv_id_from_request(request)
        source_uri = _first_non_empty_text(
            request.inputs.get("source_uri"),
            request.data.get("source_uri"),
        )

        applied = False
        if arxiv_id:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                text=arxiv_id,
                lang="en-NZ",
                context={"name_type": "CODE", "source": "scholarly_identity_assert"},
            )
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                text=f"https://arxiv.org/abs/{arxiv_id}",
                lang="en-NZ",
                context={"name_type": "CODE", "source": "scholarly_identity_assert"},
            )
            applied = True

        if source_uri:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                text=source_uri,
                lang="en-NZ",
                context={"name_type": "CODE", "source": "scholarly_identity_assert"},
            )
            applied = True

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "arxiv_id_asserted": bool(arxiv_id),
                "source_uri_asserted": bool(source_uri),
                "identity_assertions_applied": applied,
            },
        )

    return _handle


def _build_arxiv_normalise_source_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        arxiv_id = _extract_arxiv_id_from_request(request)
        if not arxiv_id:
            return WorkflowActionResult(
                status="failed",
                error="arxiv_identifier_missing",
            )

        return WorkflowActionResult(
            status="success",
            outputs={
                "result": True,
                "arxiv_id": arxiv_id,
                "source_uri": _first_non_empty_text(
                    request.inputs.get("source_uri"),
                    request.data.get("source_uri"),
                    request.inputs.get("prompt"),
                    request.data.get("prompt"),
                ),
                "normalised_arxiv_source": True,
                "verification_profile": "arxiv",
            },
        )

    return _handle


def _build_arxiv_inspect_existing_state_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        arxiv_id = _extract_arxiv_id_from_request(request)
        if not arxiv_id:
            return WorkflowActionResult(
                status="failed",
                error="arxiv_identifier_missing",
            )

        # Look for existing paper concept by arXiv ID
        search_results = concept_service.search_concepts(
            query=arxiv_id,
            match_type="substring",
            filter_kind=["individual"],
            instance_of="#V#scholarly_article",
        )

        paper_concept_id = None
        for result in search_results:
            cid = result.get("concept_id")
            if not cid:
                continue
            # Double check it matches
            if arxiv_id.lower() in cid.lower():
                paper_concept_id = cid
                break

        file_copy_concept_id = None
        if paper_concept_id:
            paper_doc = _get_concept(paper_concept_id)
            targets = _relationship_targets(
                paper_doc, "#V#propositional_information_thing_has_computer_file"
            )
            if targets:
                file_copy_concept_id = targets[0]

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "file_copy_concept_id": file_copy_concept_id,
                "arxiv_id": arxiv_id,
                "inspected_existing_state": True,
            },
        )

    return _handle


def _build_arxiv_decide_acquisition_mode_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        from ...integrations.internal_mcp.arxiv_proxy_mcp import (
            inspect_cached_arxiv_artifacts,
        )

        file_copy_concept_id = _first_non_empty_text(
            request.inputs.get("file_copy_concept_id"),
            request.data.get("file_copy_concept_id"),
        )
        arxiv_id = _extract_arxiv_id_from_request(request)
        if not arxiv_id:
            return WorkflowActionResult(
                status="failed",
                error="arxiv_identifier_missing",
            )

        cache_diagnostics = inspect_cached_arxiv_artifacts(arxiv_id=arxiv_id)
        cache_state = _clean_text(cache_diagnostics.get("cache_state")) or "cache_miss"
        can_register_file_copy = bool(_resolve_user_concept_id(request))

        if file_copy_concept_id:
            acquisition_mode = ARXIV_ACQUISITION_MODE_EXISTING_FILE_COPY
            acquisition_required = False
        elif can_register_file_copy and bool(cache_diagnostics.get("has_cached_pdf")):
            acquisition_mode = ARXIV_ACQUISITION_MODE_FINALISE_CACHED_PDF
            acquisition_required = True
        elif cache_state == "markdown_only_partial_cache":
            acquisition_mode = ARXIV_ACQUISITION_MODE_REACQUIRE_PARTIAL_CACHE
            acquisition_required = True
        else:
            acquisition_mode = ARXIV_ACQUISITION_MODE_DOWNLOAD_FROM_SOURCE
            acquisition_required = True

        return WorkflowActionResult(
            status="success",
            outputs={
                "result": acquisition_required,
                "acquisition_required": acquisition_required,
                "arxiv_id": arxiv_id,
                "file_copy_concept_id": file_copy_concept_id,
                "acquisition_mode": acquisition_mode,
                "cache_state": cache_state,
                "cache_diagnostics": cache_diagnostics,
                "cached_pdf_path": cache_diagnostics.get("cached_pdf_path"),
                "cached_markdown_path": cache_diagnostics.get("cached_markdown_path"),
                "partial_cache_without_pdf": bool(
                    cache_diagnostics.get("partial_cache_without_pdf")
                ),
                "can_register_file_copy": can_register_file_copy,
            },
        )

    return _handle


def _build_arxiv_build_completion_report_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        arxiv_id = _extract_arxiv_id_from_request(request)
        paper_concept_id = _first_non_empty_text(
            request.inputs.get("paper_concept_id"),
            request.data.get("paper_concept_id"),
        )
        file_copy_concept_id = _first_non_empty_text(
            request.inputs.get("file_copy_concept_id"),
            request.data.get("file_copy_concept_id"),
        )
        verification_failures = _coerce_string_list(
            request.inputs.get("verification_failures")
            or request.data.get("verification_failures")
        )
        verified = bool(
            request.inputs.get("scholarly_representation_verified")
            or request.data.get("scholarly_representation_verified")
        )

        report = build_arxiv_ingestion_completion_report(
            source_uri=_first_non_empty_text(
                request.inputs.get("source_uri"),
                request.data.get("source_uri"),
            ),
            arxiv_id=arxiv_id,
            metadata_status="fetched" if _extract_metadata_from_context(request) else "missing",
            acquisition_mode=_first_non_empty_text(
                request.inputs.get("acquisition_mode"),
                request.data.get("acquisition_mode"),
            ),
            download_attempted=bool(
                request.inputs.get("download_attempted") or request.data.get("download_attempted")
            ),
            download_succeeded=bool(
                request.inputs.get("download_succeeded") or request.data.get("download_succeeded")
            ),
            used_existing_file_copy=bool(
                request.inputs.get("used_existing_file_copy")
                or request.data.get("used_existing_file_copy")
            ),
            recovered_partial_state=bool(
                request.inputs.get("recovered_partial_state")
                or request.data.get("recovered_partial_state")
            ),
            file_copy_concept_id=file_copy_concept_id,
            paper_concept_id=paper_concept_id,
            author_concept_ids=_coerce_string_list(
                request.inputs.get("author_concept_ids") or request.data.get("author_concept_ids")
            ),
            topic_concept_ids=_coerce_string_list(
                request.inputs.get("topic_concept_ids") or request.data.get("topic_concept_ids")
            ),
            verification_status="verified" if verified else "failed" if verification_failures else "unknown",
            verification_failures=verification_failures,
            user_safe_operational_summary=f"Processed arXiv paper {arxiv_id}.",
        )

        return WorkflowActionResult(
            status="success",
            outputs={
                "completion_report": report,
                "arxiv_ingestion_completed": True,
            },
        )

    return _handle


def register_paper_representation_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
            handler=_build_normalise_inputs_handler(),
            description="Normalise scholarly-paper workflow inputs.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_ENSURE_PAPER_CONCEPT_ACTION_ID,
            handler=_build_scholarly_paper_ensure_paper_concept_handler(),
            description="Ensure a scholarly-paper concept instance exists.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_LINK_FILE_COPY_ACTION_ID,
            handler=_build_scholarly_paper_link_file_copy_handler(),
            description="Link a paper concept to its computer file copy.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_ATTACH_METADATA_ACTION_ID,
            handler=_build_scholarly_paper_attach_metadata_handler(),
            description="Attach core metadata (title, summary, date) to a paper concept.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
            handler=_build_materialise_from_file_copy_handler(),
            description="Materialise a scholarly-paper concept from a file copy (monolithic).",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_ENRICH_ACTION_ID,
            handler=_build_enrich_from_metadata_handler(),
            description="Enrich a scholarly-paper concept with metadata text.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
            handler=_build_resolve_authors_handler(),
            description="Resolve or create author concepts for a scholarly paper.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_RESOLVE_TOPICS_ACTION_ID,
            handler=_build_scholarly_paper_resolve_topics_handler(),
            description="Resolve or create topic concepts for a scholarly paper.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_ASSERT_SOURCE_IDENTITY_ACTION_ID,
            handler=_build_scholarly_paper_assert_source_identity_handler(),
            description="Assert source-specific identity (e.g. arXiv ID) for a paper.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_VERIFY_ACTION_ID,
            handler=_build_verify_representation_handler(),
            description="Verify scholarly-paper representation postconditions.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=ARXIV_NORMALISE_SOURCE_ACTION_ID,
            handler=_build_arxiv_normalise_source_handler(),
            description="Extract a canonical arXiv identifier from workflow inputs.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=ARXIV_INSPECT_EXISTING_STATE_ACTION_ID,
            handler=_build_arxiv_inspect_existing_state_handler(),
            description="Inspect Vontology for existing arXiv paper state.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
            handler=_build_arxiv_decide_acquisition_mode_handler(),
            description="Decide whether an arXiv workflow must acquire a file copy.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=ARXIV_BUILD_COMPLETION_REPORT_ACTION_ID,
            handler=_build_arxiv_build_completion_report_handler(),
            description="Aggregate ingestion outcomes into a structured completion report.",
        )
    )


__all__ = [
    "ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID",
    "ARXIV_NORMALISE_SOURCE_ACTION_ID",
    "SCHOLARLY_PAPER_ENRICH_ACTION_ID",
    "SCHOLARLY_PAPER_MATERIALISE_ACTION_ID",
    "SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID",
    "SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID",
    "SCHOLARLY_PAPER_VERIFY_ACTION_ID",
    "register_paper_representation_actions",
]
