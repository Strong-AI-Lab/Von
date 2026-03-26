"""Reusable arXiv-ingestion testing helpers for workflow-driven acceptance.

These helpers let canonical testing workflows prepare an isolated arXiv fixture,
verify that the real ingestion workflow represented the expected scholarly
metadata, and clean up transient artefacts afterwards.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..security.access_control import bypass_access_control
from . import concept_service
from .arxiv_metadata_service import ArxivMetadataError, fetch_arxiv_metadata
from .arxiv_paper_link_service import (
    extract_arxiv_id_candidates,
    extract_scholarly_author_names,
    extract_scholarly_metadata_publication_date,
    extract_scholarly_metadata_summary,
    extract_scholarly_metadata_title,
    extract_scholarly_topic_labels,
    predict_arxiv_paper_concept_id,
    predict_scholarly_author_concept_id,
    predict_scholarly_topic_concept_id,
)
from .computer_file_copy_service import delete_file_copy_blob_and_concept
from .text_value_service import get_texts_for_concept

_ARXIV_ABS_URL_TEMPLATE = "https://arxiv.org/abs/{arxiv_id}"
_AUTHORED_BY_PREDICATE_ID = "#V#authored_by"
_ABOUT_PREDICATE_ID = "#V#about"
_FILE_COPY_LINK_PREDICATE_ID = "#V#propositional_information_thing_has_computer_file"
_PUBLICATION_DATE_PREDICATE_ID = "#V#has_publication_date"


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _normalise_text(value: Any) -> str | None:
    text = _safe_str(value)
    if not text:
        return None
    return " ".join(text.split())


def _normalise_string_list(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _safe_str(item)
        if not text:
            continue
        lowered = text.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
    return items


def _coalesce_unique_ids(*value_groups: Any) -> list[str]:
    combined: list[str] = []
    seen: set[str] = set()
    for values in value_groups:
        for item in _normalise_string_list(values):
            lowered = item.casefold()
            if lowered in seen:
                continue
            seen.add(lowered)
            combined.append(item)
    return combined


def _relationship_targets(
    concept_doc: Mapping[str, Any] | None,
    predicate: str,
) -> list[str]:
    relationships = (concept_doc or {}).get("relationships") or {}
    raw_targets = relationships.get(predicate) or []
    if isinstance(raw_targets, str):
        return [raw_targets] if raw_targets.strip() else []
    if not isinstance(raw_targets, Sequence) or isinstance(
        raw_targets, (str, bytes, bytearray)
    ):
        return []
    targets: list[str] = []
    seen: set[str] = set()
    for item in raw_targets:
        text = _safe_str(item)
        if not text:
            continue
        lowered = text.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        targets.append(text)
    return targets


def _concept_exists(concept_id: str | None) -> bool:
    concept = _get_concept_or_none(concept_id)
    return isinstance(concept, Mapping)


def _get_concept_or_none(concept_id: str | None) -> Mapping[str, Any] | None:
    concept_text = _safe_str(concept_id)
    if not concept_text:
        return None
    try:
        # Testing workflows must verify authoritative KB state rather than the
        # caller's request-scoped visibility subset.
        with bypass_access_control():
            concept = concept_service.get_concept_by_concept_id_exact(concept_text)
    except Exception:
        return None
    if isinstance(concept, Mapping):
        return concept
    return None


def _get_text_values(
    concept_id: str | None,
    *,
    predicate: str,
    limit: int = 50,
) -> list[str]:
    concept_text = _safe_str(concept_id)
    if not concept_text:
        return []
    try:
        # Acceptance checks should read canonical text relations even when the
        # invoking UI user would not normally have direct visibility into a
        # shared concept.
        with bypass_access_control():
            rows = get_texts_for_concept(
                subject_concept_id=concept_text,
                predicate=predicate,
                limit=limit,
            )
    except Exception:
        return []
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = _normalise_text(row.get("text"))
        if not text:
            continue
        lowered = text.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        values.append(text)
    return values


def _collect_text_values(
    concept_ids: Sequence[Any],
    *,
    predicate: str,
    limit_per_concept: int = 50,
) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for concept_id in _normalise_string_list(concept_ids):
        for item in _get_text_values(
            concept_id,
            predicate=predicate,
            limit=limit_per_concept,
        ):
            text = _normalise_text(item)
            if not text:
                continue
            lowered = text.casefold()
            if lowered in seen:
                continue
            seen.add(lowered)
            values.append(text)
    return values


def _first_matching_text(
    candidates: Sequence[str],
    *,
    expected: str | None,
) -> bool:
    expected_text = _normalise_text(expected)
    if not expected_text:
        return False
    expected_folded = expected_text.casefold()
    for item in candidates:
        candidate_text = _normalise_text(item)
        if candidate_text and candidate_text.casefold() == expected_folded:
            return True
    return False


def _build_observation(
    *,
    label: str,
    passed: bool,
    expected_outcome: str,
    observed_outcome: str,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "label": label,
        "verdict": "pass" if passed else "fail",
        "expected_outcome": expected_outcome,
        "observed_outcome": observed_outcome,
    }
    if isinstance(evidence, Mapping) and evidence:
        payload["evidence"] = dict(evidence)
    return payload


def _join_observed_outcome(parts: Sequence[str]) -> str:
    filtered = [item for item in (_safe_str(part) for part in parts) if item]
    return ", ".join(filtered) if filtered else "no additional detail"


def _build_existing_arxiv_cleanup_targets(
    *,
    paper_concept_id: str | None,
    expected_author_concept_ids: Sequence[Any] = (),
    expected_topic_concept_ids: Sequence[Any] = (),
) -> dict[str, Any]:
    resolved_paper_concept_id = _safe_str(paper_concept_id) or None
    represented_paper = _get_concept_or_none(resolved_paper_concept_id)
    represented_author_concept_ids = _relationship_targets(
        represented_paper,
        _AUTHORED_BY_PREDICATE_ID,
    )
    represented_topic_concept_ids = _relationship_targets(
        represented_paper,
        _ABOUT_PREDICATE_ID,
    )
    represented_file_copy_ids = _relationship_targets(
        represented_paper,
        _FILE_COPY_LINK_PREDICATE_ID,
    )
    cleanup_file_copy_ids = _coalesce_unique_ids(represented_file_copy_ids)
    cleanup_author_ids = _coalesce_unique_ids(
        represented_author_concept_ids,
        expected_author_concept_ids,
    )
    cleanup_topic_ids = _coalesce_unique_ids(
        represented_topic_concept_ids,
        expected_topic_concept_ids,
    )
    return {
        "paper_concept_id": resolved_paper_concept_id,
        "file_copy_concept_id": cleanup_file_copy_ids[0] if cleanup_file_copy_ids else None,
        "file_copy_concept_ids": cleanup_file_copy_ids,
        "author_concept_ids": cleanup_author_ids,
        "topic_concept_ids": cleanup_topic_ids,
    }


def _reclaim_existing_arxiv_test_artifacts(
    *,
    paper_concept_id: str,
    expected_author_concept_ids: Sequence[Any] = (),
    expected_topic_concept_ids: Sequence[Any] = (),
) -> dict[str, Any]:
    cleanup_targets = _build_existing_arxiv_cleanup_targets(
        paper_concept_id=paper_concept_id,
        expected_author_concept_ids=expected_author_concept_ids,
        expected_topic_concept_ids=expected_topic_concept_ids,
    )
    cleanup_result = cleanup_arxiv_paper_ingestion_test_artifacts(
        paper_concept_id=cleanup_targets.get("paper_concept_id"),
        file_copy_concept_id=cleanup_targets.get("file_copy_concept_id"),
        file_copy_concept_ids=cleanup_targets.get("file_copy_concept_ids") or (),
        author_concept_ids=cleanup_targets.get("author_concept_ids") or (),
        topic_concept_ids=cleanup_targets.get("topic_concept_ids") or (),
        # Preserve shared author/topic concepts while reclaiming the blocking
        # paper/file-copy artefacts from a prior run of the deterministic test.
        preexisting_author_concept_ids=cleanup_targets.get("author_concept_ids") or (),
        preexisting_topic_concept_ids=cleanup_targets.get("topic_concept_ids") or (),
    )
    blocked_concept_ids = [
        concept_id
        for concept_id in _coalesce_unique_ids(
            cleanup_targets.get("paper_concept_id"),
            cleanup_targets.get("file_copy_concept_ids") or (),
        )
        if _concept_exists(concept_id)
    ]
    reclamation_passed = bool(cleanup_result.get("cleanup_passed")) and not blocked_concept_ids
    return {
        "attempted": True,
        "reclamation_passed": reclamation_passed,
        "blocked_concept_ids": blocked_concept_ids,
        "cleanup_targets": cleanup_targets,
        "cleanup_result": cleanup_result,
    }


def prepare_arxiv_paper_ingestion_test_fixture(
    *,
    prompt_text: str | None = None,
    arxiv_source: str | None = None,
    source_uri: str | None = None,
    arxiv_id: str | None = None,
    user_concept_id: str | None = None,
    timeout_seconds: float = 15.0,
    repair_existing_artifacts: bool = False,
) -> dict[str, Any]:
    """Prepare an isolated arXiv-ingestion fixture for a testing workflow."""

    arxiv_candidates = extract_arxiv_id_candidates(
        arxiv_id,
        arxiv_source,
        source_uri,
        prompt_text,
    )
    if not arxiv_candidates:
        return {
            "success": False,
            "error": "arxiv_identifier_missing",
        }

    resolved_arxiv_id = arxiv_candidates[0]
    resolved_source_uri = _ARXIV_ABS_URL_TEMPLATE.format(arxiv_id=resolved_arxiv_id)
    resolved_prompt_text = (
        _safe_str(prompt_text)
        or _safe_str(arxiv_source)
        or _safe_str(source_uri)
        or resolved_source_uri
    )

    try:
        metadata = fetch_arxiv_metadata(
            resolved_arxiv_id,
            timeout_seconds=float(timeout_seconds),
        )
    except ArxivMetadataError as exc:
        return {
            "success": False,
            "error": str(exc),
            "arxiv_id": resolved_arxiv_id,
            "source_uri": resolved_source_uri,
        }

    expected_title = extract_scholarly_metadata_title(metadata)
    expected_summary = extract_scholarly_metadata_summary(metadata)
    expected_publication_date = extract_scholarly_metadata_publication_date(metadata)
    expected_author_names = extract_scholarly_author_names(metadata)
    expected_topic_labels = extract_scholarly_topic_labels(metadata)
    versioned_id = _safe_str(metadata.get("versioned_id"))
    original_filename = f"{versioned_id or resolved_arxiv_id}.pdf"
    resolved_user_concept_id = _safe_str(user_concept_id) or "#V#anonymous"

    expected_author_concept_ids = [
        predict_scholarly_author_concept_id(
            user_concept_id=resolved_user_concept_id,
            author_name=author_name,
        )
        for author_name in expected_author_names
    ]
    expected_topic_concept_ids = [
        predict_scholarly_topic_concept_id(
            user_concept_id=resolved_user_concept_id,
            topic_label=topic_label,
        )
        for topic_label in expected_topic_labels
    ]
    paper_concept_id = predict_arxiv_paper_concept_id(arxiv_id=resolved_arxiv_id)
    stale_artifact_reclamation: dict[str, Any] | None = None
    if _concept_exists(paper_concept_id):
        if not repair_existing_artifacts:
            return {
                "success": False,
                "error": "paper_concept_already_exists",
                "arxiv_id": resolved_arxiv_id,
                "source_uri": resolved_source_uri,
                "paper_concept_id": paper_concept_id,
            }
        stale_artifact_reclamation = _reclaim_existing_arxiv_test_artifacts(
            paper_concept_id=paper_concept_id,
            expected_author_concept_ids=expected_author_concept_ids,
            expected_topic_concept_ids=expected_topic_concept_ids,
        )
        if not stale_artifact_reclamation.get("reclamation_passed"):
            return {
                "success": False,
                "error": "paper_concept_reclamation_failed",
                "arxiv_id": resolved_arxiv_id,
                "source_uri": resolved_source_uri,
                "paper_concept_id": paper_concept_id,
                "stale_artifact_reclamation": stale_artifact_reclamation,
            }

    preexisting_author_concept_ids = [
        concept_id
        for concept_id in expected_author_concept_ids
        if _concept_exists(concept_id)
    ]
    preexisting_topic_concept_ids = [
        concept_id
        for concept_id in expected_topic_concept_ids
        if _concept_exists(concept_id)
    ]

    return {
        "success": True,
        "schema_version": "arxiv_ingestion_test_fixture.v1",
        "arxiv_id": resolved_arxiv_id,
        "source_uri": resolved_source_uri,
        "prompt_text": resolved_prompt_text,
        "original_filename": original_filename,
        "paper_metadata": dict(metadata),
        "paper_concept_id": paper_concept_id,
        "expected_title": expected_title,
        "expected_summary": expected_summary,
        "expected_publication_date": expected_publication_date,
        "expected_author_names": expected_author_names,
        "expected_author_concept_ids": expected_author_concept_ids,
        "expected_topic_labels": expected_topic_labels,
        "expected_topic_concept_ids": expected_topic_concept_ids,
        "preexisting_author_concept_ids": preexisting_author_concept_ids,
        "preexisting_topic_concept_ids": preexisting_topic_concept_ids,
        "cleanup_plan": {
            "paper_concept_id": paper_concept_id,
            "author_concept_ids": expected_author_concept_ids,
            "topic_concept_ids": expected_topic_concept_ids,
            "preexisting_author_concept_ids": preexisting_author_concept_ids,
            "preexisting_topic_concept_ids": preexisting_topic_concept_ids,
        },
        "stale_artifact_reclamation": stale_artifact_reclamation,
    }


def verify_arxiv_paper_ingestion_test_result(
    *,
    workflow_execution: Mapping[str, Any] | None,
    arxiv_id: str | None,
    source_uri: str | None,
    expected_title: str | None,
    expected_summary: str | None,
    expected_publication_date: str | None,
    expected_author_names: Sequence[Any] = (),
    expected_author_concept_ids: Sequence[Any] = (),
    expected_topic_labels: Sequence[Any] = (),
    expected_topic_concept_ids: Sequence[Any] = (),
    paper_concept_id: str | None = None,
) -> dict[str, Any]:
    """Verify represented paper metadata, links, and provenance after execution."""

    workflow_execution_payload = (
        dict(workflow_execution) if isinstance(workflow_execution, Mapping) else {}
    )
    workflow_outputs = workflow_execution_payload.get("outputs")
    workflow_outputs = (
        dict(workflow_outputs) if isinstance(workflow_outputs, Mapping) else {}
    )

    resolved_paper_concept_id = (
        _safe_str(workflow_outputs.get("paper_concept_id")) or _safe_str(paper_concept_id)
    )
    file_copy_concept_id = _safe_str(workflow_outputs.get("file_copy_concept_id")) or None
    final_status = (
        _safe_str(workflow_execution_payload.get("final_status"))
        or _safe_str(workflow_execution_payload.get("current_status"))
        or "unknown"
    )
    represented_paper = _get_concept_or_none(resolved_paper_concept_id)
    represented_author_concept_ids = _relationship_targets(
        represented_paper,
        _AUTHORED_BY_PREDICATE_ID,
    )
    represented_topic_concept_ids = _relationship_targets(
        represented_paper,
        _ABOUT_PREDICATE_ID,
    )
    represented_file_copy_ids = _relationship_targets(
        represented_paper,
        _FILE_COPY_LINK_PREDICATE_ID,
    )
    cleanup_file_copy_ids = _coalesce_unique_ids(
        file_copy_concept_id,
        represented_file_copy_ids,
    )
    resolved_file_copy_concept_id = file_copy_concept_id or (
        cleanup_file_copy_ids[0] if cleanup_file_copy_ids else None
    )

    name_values = _get_text_values(resolved_paper_concept_id, predicate="hasName")
    description_values = _get_text_values(
        resolved_paper_concept_id,
        predicate="hasDescription",
        limit=10,
    )
    publication_date_values = _get_text_values(
        resolved_paper_concept_id,
        predicate=_PUBLICATION_DATE_PREDICATE_ID,
        limit=10,
    )

    title_matched = _first_matching_text(name_values, expected=expected_title)
    summary_matched = _first_matching_text(description_values, expected=expected_summary)
    publication_date_matched = _first_matching_text(
        publication_date_values,
        expected=expected_publication_date,
    )

    expected_arxiv_id = _normalise_text(arxiv_id)
    expected_source_uri_text = _normalise_text(source_uri)
    provenance_identifiers = {
        item.casefold()
        for item in name_values
        if _normalise_text(item)
    }
    id_preserved = bool(expected_arxiv_id) and expected_arxiv_id.casefold() in provenance_identifiers
    source_uri_preserved = (
        bool(expected_source_uri_text)
        and expected_source_uri_text.casefold() in provenance_identifiers
    )
    file_copy_link_preserved = bool(
        resolved_file_copy_concept_id
        and resolved_file_copy_concept_id in represented_file_copy_ids
        and _concept_exists(resolved_file_copy_concept_id)
    )

    expected_author_name_list = _normalise_string_list(expected_author_names)
    expected_author_ids = _normalise_string_list(expected_author_concept_ids)
    expected_topic_id_list = _normalise_string_list(expected_topic_concept_ids)
    expected_topic_label_list = _normalise_string_list(expected_topic_labels)

    represented_author_names = _collect_text_values(
        _coalesce_unique_ids(
            represented_author_concept_ids,
            expected_author_ids,
        ),
        predicate="hasName",
    )
    author_name_matches = {
        expected_author_name: _first_matching_text(
            represented_author_names,
            expected=expected_author_name,
        )
        for expected_author_name in expected_author_name_list
    }

    author_ids_matched = all(
        expected_author_id in represented_author_concept_ids
        for expected_author_id in expected_author_ids
    )
    author_names_matched = (
        all(author_name_matches.values())
        if author_name_matches
        else bool(expected_author_ids)
    )
    topic_ids_matched = all(
        expected_topic_id in represented_topic_concept_ids
        for expected_topic_id in expected_topic_id_list
    )

    metadata_observed_parts: list[str] = []
    if final_status == "completed":
        metadata_observed_parts.append("target workflow completed")
    else:
        metadata_observed_parts.append(f"target workflow status={final_status}")
    metadata_observed_parts.append(
        "title matched" if title_matched else "title missing or mismatched"
    )
    metadata_observed_parts.append(
        "abstract matched" if summary_matched else "abstract missing or mismatched"
    )
    metadata_observed_parts.append(
        "publication date matched"
        if publication_date_matched
        else "publication date missing or mismatched"
    )

    author_observed_parts: list[str] = []
    if author_ids_matched:
        author_observed_parts.append("all expected author links present")
    else:
        missing_author_ids = [
            author_id
            for author_id in expected_author_ids
            if author_id not in represented_author_concept_ids
        ]
        author_observed_parts.append(
            f"missing author links: {', '.join(missing_author_ids) or 'none'}"
        )
    if author_names_matched:
        author_observed_parts.append("author names preserved")
    else:
        author_observed_parts.append("author names missing or mismatched")
    if expected_topic_label_list:
        author_observed_parts.append(
            "topic links present" if topic_ids_matched else "topic links missing"
        )

    provenance_observed_parts: list[str] = []
    provenance_observed_parts.append(
        "arXiv identifier preserved" if id_preserved else "arXiv identifier missing"
    )
    provenance_observed_parts.append(
        "source URI preserved" if source_uri_preserved else "source URI missing"
    )
    provenance_observed_parts.append(
        "file copy linked" if file_copy_link_preserved else "file copy missing or unlinked"
    )

    metadata_passed = (
        final_status == "completed"
        and bool(resolved_paper_concept_id)
        and title_matched
        and summary_matched
        and publication_date_matched
    )
    author_links_passed = author_ids_matched and author_names_matched and (
        topic_ids_matched if expected_topic_id_list else True
    )
    provenance_passed = id_preserved and source_uri_preserved and file_copy_link_preserved

    metadata_verification = {
        "paper_concept_id": resolved_paper_concept_id or None,
        "file_copy_concept_id": resolved_file_copy_concept_id,
        "file_copy_concept_ids": cleanup_file_copy_ids,
        "final_status": final_status,
        "title_matched": title_matched,
        "summary_matched": summary_matched,
        "publication_date_matched": publication_date_matched,
        "author_ids_matched": author_ids_matched,
        "author_names_matched": author_names_matched,
        "topic_ids_matched": topic_ids_matched,
        "arxiv_identifier_preserved": id_preserved,
        "source_uri_preserved": source_uri_preserved,
        "file_copy_link_preserved": file_copy_link_preserved,
        "represented_author_concept_ids": represented_author_concept_ids,
        "represented_author_names": represented_author_names,
        "author_name_matches": author_name_matches,
        "represented_topic_concept_ids": represented_topic_concept_ids,
        "represented_file_copy_ids": represented_file_copy_ids,
    }

    return {
        "success": True,
        "verification_passed": metadata_passed and author_links_passed and provenance_passed,
        "paper_concept_id": resolved_paper_concept_id or None,
        "file_copy_concept_id": resolved_file_copy_concept_id,
        "file_copy_concept_ids": cleanup_file_copy_ids,
        "author_concept_ids": represented_author_concept_ids or expected_author_ids,
        "topic_concept_ids": represented_topic_concept_ids or expected_topic_id_list,
        "metadata_verification": metadata_verification,
        "cleanup_targets": {
            "paper_concept_id": resolved_paper_concept_id or None,
            "file_copy_concept_id": resolved_file_copy_concept_id,
            "file_copy_concept_ids": cleanup_file_copy_ids,
            "author_concept_ids": represented_author_concept_ids or expected_author_ids,
            "topic_concept_ids": represented_topic_concept_ids or expected_topic_id_list,
        },
        "observations": [
            _build_observation(
                label="metadata_representation",
                passed=metadata_passed,
                expected_outcome="title, authors, abstract, and publication date represented",
                observed_outcome=_join_observed_outcome(metadata_observed_parts),
                evidence=metadata_verification,
            ),
            _build_observation(
                label="author_links",
                passed=author_links_passed,
                expected_outcome="all expected authors and topics linked to the paper",
                observed_outcome=_join_observed_outcome(author_observed_parts),
                evidence={
                    "expected_author_concept_ids": expected_author_ids,
                    "represented_author_concept_ids": represented_author_concept_ids,
                    "expected_topic_concept_ids": expected_topic_id_list,
                    "represented_topic_concept_ids": represented_topic_concept_ids,
                },
            ),
            _build_observation(
                label="provenance_preserved",
                passed=provenance_passed,
                expected_outcome="arXiv identifiers, source URL, and file copy link preserved",
                observed_outcome=_join_observed_outcome(provenance_observed_parts),
                evidence={
                    "arxiv_id": expected_arxiv_id,
                    "source_uri": expected_source_uri_text,
                    "file_copy_concept_id": resolved_file_copy_concept_id,
                    "represented_file_copy_ids": represented_file_copy_ids,
                },
            ),
        ],
    }


def _delete_concept_if_present(concept_id: str | None) -> tuple[bool, str | None]:
    concept_text = _safe_str(concept_id)
    if not concept_text or not _concept_exists(concept_text):
        return True, None
    try:
        deleted = bool(concept_service.delete_concept(concept_text))
    except Exception as exc:
        return False, str(exc)
    return deleted, None if deleted else "delete_returned_false"


def cleanup_arxiv_paper_ingestion_test_artifacts(
    *,
    paper_concept_id: str | None,
    file_copy_concept_id: str | None,
    file_copy_concept_ids: Sequence[Any] = (),
    author_concept_ids: Sequence[Any] = (),
    topic_concept_ids: Sequence[Any] = (),
    preexisting_author_concept_ids: Sequence[Any] = (),
    preexisting_topic_concept_ids: Sequence[Any] = (),
) -> dict[str, Any]:
    """Remove transient artefacts created by an isolated arXiv-ingestion test."""

    deleted_concept_ids: list[str] = []
    skipped_preexisting_concept_ids: list[str] = []
    failed_deletions: list[str] = []
    file_copy_cleanup: dict[str, Any] | None = None
    file_copy_cleanup_records: list[dict[str, Any]] = []

    resolved_paper_concept_id = _safe_str(paper_concept_id) or None
    resolved_file_copy_concept_ids = _coalesce_unique_ids(
        file_copy_concept_id,
        file_copy_concept_ids,
    )
    resolved_file_copy_concept_id = (
        resolved_file_copy_concept_ids[0] if resolved_file_copy_concept_ids else None
    )
    expected_preexisting_author_ids = set(_normalise_string_list(preexisting_author_concept_ids))
    expected_preexisting_topic_ids = set(_normalise_string_list(preexisting_topic_concept_ids))

    if resolved_paper_concept_id:
        paper_deleted, paper_error = _delete_concept_if_present(resolved_paper_concept_id)
        if paper_deleted:
            deleted_concept_ids.append(resolved_paper_concept_id)
        elif paper_error:
            failed_deletions.append(
                f"{resolved_paper_concept_id}:concept_delete_failed:{paper_error}"
            )

    if not failed_deletions:
        for resolved_file_copy_concept_id in resolved_file_copy_concept_ids:
            cleanup_record: dict[str, Any]
            if _concept_exists(resolved_file_copy_concept_id):
                cleanup_record = delete_file_copy_blob_and_concept(
                    file_copy_concept_id=resolved_file_copy_concept_id
                )
                if bool(cleanup_record.get("success")):
                    deleted_concept_ids.append(resolved_file_copy_concept_id)
                else:
                    failed_deletions.append(
                        f"{resolved_file_copy_concept_id}:{_safe_str(cleanup_record.get('error')) or 'file_copy_cleanup_failed'}"
                    )
            else:
                cleanup_record = {
                    "success": True,
                    "message": "File copy already absent.",
                    "concept_id": resolved_file_copy_concept_id,
                    "concept_deleted": True,
                    "blob_deleted": True,
                    "partial": False,
                }
            file_copy_cleanup_records.append(cleanup_record)
        if file_copy_cleanup_records:
            file_copy_cleanup = file_copy_cleanup_records[0]

    if not failed_deletions:
        for concept_id in _normalise_string_list(author_concept_ids):
            if concept_id in expected_preexisting_author_ids:
                skipped_preexisting_concept_ids.append(concept_id)
                continue
            deleted, error = _delete_concept_if_present(concept_id)
            if deleted:
                deleted_concept_ids.append(concept_id)
            elif error:
                failed_deletions.append(f"{concept_id}:concept_delete_failed:{error}")

        for concept_id in _normalise_string_list(topic_concept_ids):
            if concept_id in expected_preexisting_topic_ids:
                skipped_preexisting_concept_ids.append(concept_id)
                continue
            deleted, error = _delete_concept_if_present(concept_id)
            if deleted:
                deleted_concept_ids.append(concept_id)
            elif error:
                failed_deletions.append(f"{concept_id}:concept_delete_failed:{error}")

    cleanup_passed = len(failed_deletions) == 0
    observed_parts: list[str] = []
    if deleted_concept_ids:
        observed_parts.append(f"deleted {len(deleted_concept_ids)} transient concepts")
    if skipped_preexisting_concept_ids:
        observed_parts.append(
            f"preserved {len(skipped_preexisting_concept_ids)} pre-existing concepts"
        )
    if failed_deletions:
        observed_parts.append(f"cleanup failures: {'; '.join(failed_deletions)}")
    if not observed_parts:
        observed_parts.append("no transient artefacts required deletion")

    cleanup_summary = {
        "cleanup_passed": cleanup_passed,
        "deleted_concept_ids": deleted_concept_ids,
        "skipped_preexisting_concept_ids": skipped_preexisting_concept_ids,
        "failed_deletions": failed_deletions,
        "paper_concept_id": resolved_paper_concept_id,
        "file_copy_concept_id": resolved_file_copy_concept_id,
        "file_copy_concept_ids": resolved_file_copy_concept_ids,
        "file_copy_cleanup": file_copy_cleanup,
        "file_copy_cleanup_records": file_copy_cleanup_records,
    }

    return {
        "success": True,
        "cleanup_passed": cleanup_passed,
        "cleanup_summary": cleanup_summary,
        "observations": [
            _build_observation(
                label="cleanup_completed",
                passed=cleanup_passed,
                expected_outcome="transient ingestion artefacts removed",
                observed_outcome=_join_observed_outcome(observed_parts),
                evidence=cleanup_summary,
            )
        ],
    }


__all__ = [
    "cleanup_arxiv_paper_ingestion_test_artifacts",
    "prepare_arxiv_paper_ingestion_test_fixture",
    "verify_arxiv_paper_ingestion_test_result",
]
