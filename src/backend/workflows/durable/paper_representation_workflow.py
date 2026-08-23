"""Reusable durable actions for scholarly-paper and arXiv workflows."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
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
    predict_scholarly_author_concept_id,
    predict_scholarly_topic_concept_id,
)
from ...services.ontology_publication_authority_service import (
    PublicationContext,
    PublicationContextKind,
    concept_publication_context,
)
from ...services.publication_scope_profile_service import (
    resolve_publication_scope_profile,
)
from ...services.relationship_write_service import (
    add_structural_relationship,
    validate_predicate_concept,
)
from ...services.scoped_assertion_service import (
    list_visible_scoped_assertions,
    upsert_scoped_assertion,
)
from ...services.text_value_service import (
    get_texts_for_concept,
    upsert_text_for_concept,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..execution_contracts import build_arxiv_ingestion_completion_report

logger = logging.getLogger(__name__)
_PUBLICATION_DATE_PREDICATE_ID = "#V#has_publication_date"
_LOCAL_FILE_COPY_PREDICATE_ID = "#V#local_file_copy"
_MAX_TOPIC_RELATIONS = 3

SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID = "scholarly_paper.normalise_inputs"
SCHOLARLY_PAPER_NORMALISE_EXTERNAL_IDENTITY_ACTION_ID = (
    "scholarly_paper.normalise_external_identity"
)
SCHOLARLY_PAPER_ENSURE_PAPER_CONCEPT_ACTION_ID = "scholarly_paper.ensure_paper_concept"
SCHOLARLY_PAPER_LINK_FILE_COPY_ACTION_ID = "scholarly_paper.link_file_copy"
SCHOLARLY_PAPER_ATTACH_METADATA_ACTION_ID = "scholarly_paper.attach_metadata"
SCHOLARLY_PAPER_MATERIALISE_ACTION_ID = "scholarly_paper.materialise_from_file_copy"
SCHOLARLY_PAPER_ENRICH_ACTION_ID = "scholarly_paper.enrich_from_metadata"
SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID = "scholarly_paper.resolve_authors"
SCHOLARLY_PAPER_RESOLVE_TOPICS_ACTION_ID = "scholarly_paper.resolve_topics"
SCHOLARLY_PAPER_ASSERT_SOURCE_IDENTITY_ACTION_ID = (
    "scholarly_paper.assert_source_identity"
)
SCHOLARLY_PAPER_VERIFY_ACTION_ID = "scholarly_paper.verify_representation"

ARXIV_NORMALISE_SOURCE_ACTION_ID = "arxiv.normalise_source"
ARXIV_INSPECT_EXISTING_STATE_ACTION_ID = "arxiv.inspect_existing_state"
ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID = "arxiv.decide_acquisition_mode"
ARXIV_BUILD_COMPLETION_REPORT_ACTION_ID = "arxiv.build_completion_report"
PAPER_REFERENCE_NORMALISE_SET_ACTION_ID = "paper_reference.normalise_reference_set"
PAPER_REFERENCE_FAIL_ITEM_ACTION_ID = "paper_reference.fail_item"
PAPER_REFERENCE_PREPARE_PUBLIC_TITLE_SEARCH_ACTION_ID = (
    "paper_reference.prepare_public_title_search"
)
PAPER_REFERENCE_SELECT_ARXIV_TITLE_MATCH_ACTION_ID = (
    "paper_reference.select_arxiv_title_match"
)

ARXIV_ACQUISITION_MODE_EXISTING_FILE_COPY = "existing_file_copy"
ARXIV_ACQUISITION_MODE_FINALISE_CACHED_PDF = "finalise_cached_pdf"
ARXIV_ACQUISITION_MODE_REACQUIRE_PARTIAL_CACHE = "reacquire_partial_cache"
ARXIV_ACQUISITION_MODE_DOWNLOAD_FROM_SOURCE = "download_from_source"

_DOI_CANDIDATE_PATTERN = re.compile(
    r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+",
    re.IGNORECASE,
)
_PAPER_REFERENCE_KIND_ALIASES = {
    "arxiv": "arxiv",
    "arxiv_id": "arxiv",
    "arxiv_url": "arxiv",
    "doi": "doi",
    "doi_url": "doi",
    "source": "source_uri",
    "source_url": "source_uri",
    "source_uri": "source_uri",
    "url": "source_uri",
    "web": "source_uri",
    "file": "file_copy",
    "file_copy": "file_copy",
    "uploaded_file": "file_copy",
    "uploaded_pdf": "file_copy",
    "pdf": "file_copy",
    "metadata": "metadata",
    "bibliographic_metadata": "metadata",
    "pasted_metadata": "metadata",
}


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


def _normalise_reference_kind(value: Any) -> str:
    text = _clean_text(value).lower().replace("-", "_").replace(" ", "_")
    return _PAPER_REFERENCE_KIND_ALIASES.get(text, text or "unknown")


def _clean_doi(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if text.lower().startswith("https://doi.org/"):
        text = text[len("https://doi.org/") :]
    elif text.lower().startswith("http://doi.org/"):
        text = text[len("http://doi.org/") :]
    return text.strip().rstrip(".,;)]}")


def _normalise_public_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean_text(value)).casefold()
    return " ".join(
        "".join(character if character.isalnum() else " " for character in text).split()
    )


def _public_search_result_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        rows = value.get("results")
        if isinstance(rows, Sequence) and not isinstance(
            rows, (str, bytes, bytearray)
        ):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
        for child in value.values():
            nested = _public_search_result_rows(child)
            if nested:
                return nested
    return []


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = _clean_text(value).lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _extract_doi_candidates(value: Any) -> list[str]:
    values: list[Any]
    if isinstance(value, Mapping):
        values = list(value.values())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = list(value)
    else:
        values = [value]

    candidates: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean_text(item)
        if not text:
            continue
        for match in _DOI_CANDIDATE_PATTERN.finditer(text):
            doi = _clean_doi(match.group(0))
            lowered = doi.casefold()
            if doi and lowered not in seen:
                seen.add(lowered)
                candidates.append(doi)
    return candidates


def _normalise_source_context(
    value: Any, *, provenance_required: bool = True
) -> dict[str, Any]:
    source_context = _coerce_mapping(value)
    if "provenance_required" not in source_context:
        source_context["provenance_required"] = provenance_required
    return source_context


def _coerce_reference_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    text = _clean_text(value)
    if not text:
        return None
    return {"source_uri": text}


def _extend_reference_items_from_value(
    result: list[dict[str, Any]],
    value: Any,
) -> None:
    if isinstance(value, Mapping):
        items = value.get("items")
        if isinstance(items, Sequence) and not isinstance(
            items, (str, bytes, bytearray)
        ):
            for item in items:
                item_mapping = _coerce_reference_mapping(item)
                if item_mapping is not None:
                    result.append(item_mapping)
            return
        item_mapping = _coerce_reference_mapping(value)
        if item_mapping is not None:
            result.append(item_mapping)
        return

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            item_mapping = _coerce_reference_mapping(item)
            if item_mapping is not None:
                result.append(item_mapping)
        return

    item_mapping = _coerce_reference_mapping(value)
    if item_mapping is not None:
        result.append(item_mapping)


def _paper_reference_source_uri_from_item(item: Mapping[str, Any]) -> str:
    return (
        _first_non_empty_text(
            item.get("source_uri"),
            item.get("source_url"),
            item.get("url"),
            item.get("uri"),
            item.get("href"),
        )
        or ""
    )


def _paper_reference_metadata_from_item(item: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("paper_metadata", "metadata", "scholarly_metadata"):
        value = item.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    metadata: dict[str, Any] = {}
    for source_key, target_key in (
        ("title", "title"),
        ("paper_title", "title"),
        ("summary", "summary"),
        ("abstract", "abstract"),
        ("publication_date", "publication_date"),
        ("doi", "doi"),
        ("source_uri", "source_uri"),
        ("source_url", "source_uri"),
    ):
        value = item.get(source_key)
        if value not in (None, "", [], {}):
            metadata[target_key] = value
    authors = _coerce_string_list(item.get("author_names") or item.get("authors"))
    if authors:
        metadata["authors"] = authors
    topics = _coerce_string_list(
        item.get("topic_labels") or item.get("keywords") or item.get("categories")
    )
    if topics:
        metadata["categories"] = topics
    return metadata


def _normalise_paper_reference_item(
    item: Mapping[str, Any],
    *,
    source_context: Mapping[str, Any],
    default_preview_only: bool,
    index: int,
) -> dict[str, Any]:
    source_uri = _paper_reference_source_uri_from_item(item)
    arxiv_id = _first_non_empty_text(item.get("arxiv_id"), item.get("arxiv"))
    if not arxiv_id:
        arxiv_candidates = extract_arxiv_id_candidates(
            [source_uri, item.get("prompt"), item.get("text")]
        )
        arxiv_id = arxiv_candidates[0] if arxiv_candidates else None

    doi = _clean_doi(_first_non_empty_text(item.get("doi"), item.get("doi_url")))
    if not doi:
        doi_candidates = _extract_doi_candidates(
            [source_uri, item.get("prompt"), item.get("text"), item.get("metadata")]
        )
        doi = doi_candidates[0] if doi_candidates else ""

    file_copy_concept_id = _first_non_empty_text(
        item.get("file_copy_concept_id"),
        item.get("source_file_copy_concept_id"),
        item.get("computer_file_copy_concept_id"),
    )
    metadata = _paper_reference_metadata_from_item(item)
    if doi and "doi" not in metadata:
        metadata["doi"] = doi
    if source_uri and "source_uri" not in metadata:
        metadata["source_uri"] = source_uri

    reference_kind = _normalise_reference_kind(item.get("reference_kind"))
    if reference_kind == "unknown":
        if arxiv_id:
            reference_kind = "arxiv"
        elif doi:
            reference_kind = "doi"
        elif file_copy_concept_id:
            reference_kind = "file_copy"
        elif metadata:
            reference_kind = "metadata"
        elif source_uri:
            reference_kind = "source_uri"

    preview_only = item.get("preview_only")
    if not isinstance(preview_only, bool):
        preview_only = default_preview_only

    normalised: dict[str, Any] = {
        "reference_index": index,
        "reference_kind": reference_kind,
        "source_context": dict(source_context),
        "provenance_required": bool(source_context.get("provenance_required", True)),
        "preview_only": preview_only,
    }
    for key, value in (
        ("arxiv_id", arxiv_id),
        ("doi", doi),
        ("source_uri", source_uri),
        ("file_copy_concept_id", file_copy_concept_id),
        ("paper_concept_id", _first_non_empty_text(item.get("paper_concept_id"))),
        ("title", _first_non_empty_text(item.get("title"), item.get("paper_title"))),
        ("summary", _first_non_empty_text(item.get("summary"), item.get("abstract"))),
        ("publication_date", _first_non_empty_text(item.get("publication_date"))),
        ("prompt", _first_non_empty_text(item.get("prompt"), item.get("text"))),
    ):
        if value not in (None, "", [], {}):
            normalised[key] = value

    authors = _coerce_string_list(item.get("author_names") or item.get("authors"))
    if authors:
        normalised["author_names"] = authors
    topics = _coerce_string_list(
        item.get("topic_labels") or item.get("keywords") or item.get("categories")
    )
    if topics:
        normalised["topic_labels"] = topics
    if metadata:
        normalised["paper_metadata"] = metadata
    if reference_kind == "unknown":
        normalised["error_code"] = "paper_reference_kind_unresolved"
        normalised["error_message"] = (
            "Reference did not include an arXiv id, DOI, source URI, file copy, "
            "or bibliographic metadata."
        )
    return normalised


def _build_reference_item_candidates(
    request: WorkflowActionRequest,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for source in (
        request.inputs.get("paper_reference_set"),
        request.data.get("paper_reference_set"),
        request.inputs.get("paper_references"),
        request.data.get("paper_references"),
        request.inputs.get("references"),
        request.data.get("references"),
        request.inputs.get("items"),
        request.data.get("items"),
        request.inputs.get("paper_reference"),
        request.data.get("paper_reference"),
    ):
        _extend_reference_items_from_value(candidates, source)

    for arxiv_id in _coerce_string_list(
        request.inputs.get("arxiv_ids") or request.data.get("arxiv_ids")
    ):
        candidates.append({"reference_kind": "arxiv", "arxiv_id": arxiv_id})
    arxiv_id = _first_non_empty_text(
        request.inputs.get("arxiv_id"),
        request.data.get("arxiv_id"),
    )
    if arxiv_id:
        candidates.append({"reference_kind": "arxiv", "arxiv_id": arxiv_id})

    doi = _first_non_empty_text(request.inputs.get("doi"), request.data.get("doi"))
    if doi:
        candidates.append({"reference_kind": "doi", "doi": doi})
    source_uri = _first_non_empty_text(
        request.inputs.get("source_uri"),
        request.inputs.get("source_url"),
        request.data.get("source_uri"),
        request.data.get("source_url"),
    )
    if source_uri:
        candidates.append({"source_uri": source_uri})
    file_copy_concept_id = _first_non_empty_text(
        request.inputs.get("file_copy_concept_id"),
        request.data.get("file_copy_concept_id"),
    )
    if file_copy_concept_id:
        candidates.append(
            {
                "reference_kind": "file_copy",
                "file_copy_concept_id": file_copy_concept_id,
            }
        )

    metadata = _extract_metadata_from_context(request)
    if metadata:
        candidates.append({"reference_kind": "metadata", "paper_metadata": metadata})

    prompt = _first_non_empty_text(
        request.inputs.get("prompt"), request.data.get("prompt")
    )
    if prompt:
        for arxiv_candidate in extract_arxiv_id_candidates(prompt):
            candidates.append({"reference_kind": "arxiv", "arxiv_id": arxiv_candidate})
        for doi_candidate in _extract_doi_candidates(prompt):
            candidates.append({"reference_kind": "doi", "doi": doi_candidate})

    return candidates


def _dedupe_reference_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        identity = _first_non_empty_text(
            item.get("arxiv_id"),
            item.get("doi"),
            item.get("file_copy_concept_id"),
            item.get("source_uri"),
            item.get("title"),
            item.get("prompt"),
        )
        key = (
            _normalise_reference_kind(item.get("reference_kind")),
            identity or str(item),
        )
        lowered_key = (key[0], key[1].casefold())
        if lowered_key in seen:
            continue
        seen.add(lowered_key)
        item["reference_index"] = len(deduped)
        deduped.append(item)
    return deduped


def _relationship_targets(
    concept_doc: Mapping[str, Any] | None, predicate: str
) -> list[str]:
    relationships = (
        concept_doc.get("relationships") if isinstance(concept_doc, Mapping) else None
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
    return bool(target_clean) and target_clean in _relationship_targets(
        concept_doc, predicate
    )


def _get_concept(concept_id: str) -> dict[str, Any] | None:
    concept_id_clean = _clean_text(concept_id)
    if not concept_id_clean:
        return None
    try:
        concept = concept_service.get_concept_by_concept_id_exact(concept_id_clean)
    except Exception:
        return None
    return concept if isinstance(concept, Mapping) else None


def _concept_exists(concept_id: str) -> bool:
    return _get_concept(concept_id) is not None


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
        namespace=_clean_text(getattr(request.environment, "user_namespace", None))
        or None,
    )
    return _clean_text(resolved_user) or None


def _trusted_actor_concept_id(request: WorkflowActionRequest) -> str | None:
    """Return only the server-bound workflow actor, never payload identity."""

    return _clean_text(getattr(request.environment, "user_concept_id", None)) or None


def _require_actor_private_targets(
    request: WorkflowActionRequest,
    concept_ids: Sequence[str],
) -> WorkflowActionResult | None:
    """Preflight every pre-existing record a custom action will mutate."""

    actor_concept_id = _trusted_actor_concept_id(request)
    if not actor_concept_id:
        return WorkflowActionResult(
            status="failed",
            error="scholarly_paper_actor_private_target_required",
        )

    expected_context = PublicationContext.user(actor_concept_id)
    checked: set[str] = set()
    for raw_concept_id in concept_ids:
        concept_id = _clean_text(raw_concept_id)
        if not concept_id or concept_id in checked:
            continue
        checked.add(concept_id)
        try:
            target_context = concept_publication_context(concept_id)
        except (LookupError, ValueError):
            return WorkflowActionResult(
                status="failed",
                outputs={"mutation_target_concept_id": concept_id},
                error="scholarly_paper_mutation_target_missing",
            )
        if (
            target_context.kind != PublicationContextKind.USER
            or target_context.concept_id != expected_context.concept_id
        ):
            return WorkflowActionResult(
                status="failed",
                outputs={
                    "mutation_target_concept_id": concept_id,
                    "mutation_target_publication_context": target_context.to_mapping(),
                },
                error="scholarly_paper_actor_private_target_required",
            )
    return None


def _require_global_targets(
    concept_ids: Sequence[str],
    *,
    error_code: str,
) -> WorkflowActionResult | None:
    """Require exact global publication for canonical public mutations."""

    checked: set[str] = set()
    for raw_concept_id in concept_ids:
        concept_id = _clean_text(raw_concept_id)
        if not concept_id or concept_id in checked:
            continue
        checked.add(concept_id)
        try:
            target_context = concept_publication_context(concept_id)
        except (LookupError, ValueError):
            return WorkflowActionResult(
                status="failed",
                outputs={"mutation_target_concept_id": concept_id},
                error="scholarly_paper_mutation_target_missing",
            )
        if target_context.kind != PublicationContextKind.GLOBAL:
            return WorkflowActionResult(
                status="failed",
                outputs={
                    "mutation_target_concept_id": concept_id,
                    "mutation_target_publication_context": (
                        target_context.to_mapping()
                    ),
                },
                error=error_code,
            )
    return None


def _paper_scope_mode_from_request(request: WorkflowActionRequest) -> str | None:
    return _first_non_empty_text(
        request.inputs.get("paper_instance_scope_mode"),
        request.data.get("paper_instance_scope_mode"),
    )


def _visible_local_file_copy_assertions(
    request: WorkflowActionRequest,
    *,
    paper_concept_id: str,
) -> list[dict[str, Any]]:
    actor_concept_id = _trusted_actor_concept_id(request)
    if not actor_concept_id:
        return []
    return list_visible_scoped_assertions(
        subject_concept_ids=[paper_concept_id],
        predicates=[_LOCAL_FILE_COPY_PREDICATE_ID],
        object_kind="concept",
        limit=50,
        user_concept_id=actor_concept_id,
        organisation_concept_id=(
            _clean_text(getattr(request.environment, "org_concept_id", None)) or None
        ),
    )


def _require_preprovisioned_schema_support(
    *,
    type_concept_ids: Sequence[str] = (),
    predicate_concept_ids: Sequence[str] = (),
) -> WorkflowActionResult | None:
    """Reject ordinary actor-private work instead of creating global schema."""

    missing = [
        concept_id
        for concept_id in (*type_concept_ids, *predicate_concept_ids)
        if not _concept_exists(concept_id)
    ]
    if missing:
        return WorkflowActionResult(
            status="failed",
            outputs={"missing_schema_concept_ids": list(dict.fromkeys(missing))},
            error="scholarly_paper_schema_support_missing",
        )

    # Predicate typing is hard schema authority rather than actor-visible domain
    # data.  Use the canonical validator: it deliberately reads that one schema
    # fact through the hard-authority view, while mutation source/target access
    # remains governed by ``_require_actor_private_targets``.
    invalid_predicates = [
        concept_id
        for concept_id in predicate_concept_ids
        if not validate_predicate_concept(concept_id)[0]
    ]
    if invalid_predicates:
        return WorkflowActionResult(
            status="failed",
            outputs={"invalid_schema_predicate_ids": invalid_predicates},
            error="scholarly_paper_schema_support_invalid",
        )
    return None


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


_PUBLIC_AUTHOR_IDENTIFIER_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("orcid", ("orcid", "orcid_id")),
    ("openalex", ("openalex", "openalex_id")),
    ("researcherid", ("researcherid", "researcher_id")),
    ("scopus", ("scopus", "scopus_id", "scopus_author_id")),
)


def _normalise_public_author_identifier(scheme: str, value: Any) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        return ""
    lowered = cleaned.casefold()
    prefixes = {
        "orcid": ("https://orcid.org/", "http://orcid.org/", "orcid:"),
        "openalex": ("https://openalex.org/", "http://openalex.org/"),
    }
    for prefix in prefixes.get(scheme, ()):
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned.casefold()


def _author_record_candidates(request: WorkflowActionRequest) -> list[dict[str, Any]]:
    metadata = _extract_metadata_from_context(request)
    raw_authors: Any = None
    for candidate in (
        request.inputs.get("author_records"),
        request.data.get("author_records"),
        metadata.get("authors"),
        request.inputs.get("author_names"),
        request.data.get("author_names"),
    ):
        if candidate not in (None, "", [], {}):
            raw_authors = candidate
            break

    if isinstance(raw_authors, str):
        raw_items: list[Any] = [
            item for item in re.split(r"[;\n]+", raw_authors) if item.strip()
        ]
    elif isinstance(raw_authors, Sequence) and not isinstance(
        raw_authors, (str, bytes, bytearray)
    ):
        raw_items = list(raw_authors)
    else:
        raw_items = []

    records: list[dict[str, Any]] = []
    for item in raw_items:
        if isinstance(item, Mapping):
            name = _first_non_empty_text(
                item.get("name"),
                item.get("full_name"),
                item.get("display_name"),
                item.get("author"),
            )
            public_identifier: dict[str, str] | None = None
            nested_identifiers = item.get("external_identifiers")
            identifier_sources = [item]
            if isinstance(nested_identifiers, Mapping):
                identifier_sources.insert(0, nested_identifiers)
            for scheme, field_names in _PUBLIC_AUTHOR_IDENTIFIER_FIELDS:
                raw_value = None
                for source in identifier_sources:
                    raw_value = next(
                        (
                            source.get(field_name)
                            for field_name in field_names
                            if source.get(field_name) not in (None, "")
                        ),
                        None,
                    )
                    if raw_value is not None:
                        break
                value = _normalise_public_author_identifier(scheme, raw_value)
                if value:
                    public_identifier = {"scheme": scheme, "value": value}
                    break
        else:
            name = _clean_text(item)
            public_identifier = None
        if name:
            records.append(
                {
                    "name": " ".join(name.split()),
                    "public_identifier": public_identifier,
                }
            )
    return records


def _public_author_records_for_paper(
    request: WorkflowActionRequest,
    *,
    paper_concept_id: str,
) -> list[dict[str, Any]]:
    from ...services.concept_external_identity_service import (
        ExternalIdentifier,
        canonical_concept_id_for_external_identifiers,
    )

    records: list[dict[str, Any]] = []
    for position, candidate in enumerate(_author_record_candidates(request)):
        name = _clean_text(candidate.get("name"))
        public_identifier = candidate.get("public_identifier")
        if isinstance(public_identifier, Mapping):
            scheme = _clean_text(public_identifier.get("scheme")).casefold()
            value = _clean_text(public_identifier.get("value"))
            identity_kind = "public_identifier"
        else:
            scheme = "scholarly-author-occurrence"
            occurrence_material = json.dumps(
                {
                    "author_name": " ".join(name.split()).casefold(),
                    "author_position": position,
                    "paper_concept_id": paper_concept_id,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            value = f"sha256:{hashlib.sha256(occurrence_material.encode('utf-8')).hexdigest()}"
            identity_kind = "source_bounded_candidate"
        identifier = ExternalIdentifier(
            scheme=scheme,
            value=value,
            source="public_scholarly_metadata",
        )
        concept_id = canonical_concept_id_for_external_identifiers(
            (identifier,),
            kind="instance",
            parent_id="#V#person",
            scope_mode="global_general",
        )
        if not concept_id:
            raise ValueError("scholarly_author_external_identity_invalid")
        records.append(
            {
                "name": name,
                "concept_id": concept_id,
                "external_identifiers": [identifier.to_dict()],
                "identity_kind": identity_kind,
                "identity_scheme": scheme,
                "identity_stable": True,
                "source_kind": "public_scholarly_metadata",
                "source_paper_concept_id": paper_concept_id,
                "source_author_position": position,
                "description": (
                    "Public scholarly author identity from stable public metadata."
                    if identity_kind == "public_identifier"
                    else (
                        "Public scholarly author candidate bounded to one paper and "
                        "author position; it is not a name-only cross-paper identity merge."
                    )
                ),
            }
        )
    return records


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


def _build_normalise_external_identity_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        paper_scope_mode = _paper_scope_mode_from_request(request)
        if paper_scope_mode != "global_general":
            return WorkflowActionResult(
                status="failed",
                outputs={"paper_instance_scope_mode": paper_scope_mode},
                error="scholarly_paper_global_scope_profile_required",
            )

        supplied_paper_concept_id = _first_non_empty_text(
            request.inputs.get("paper_concept_id"),
            request.data.get("paper_concept_id"),
        )
        if supplied_paper_concept_id:
            target_preflight = _require_global_targets(
                (supplied_paper_concept_id,),
                error_code="scholarly_paper_global_target_required",
            )
            if target_preflight is not None:
                return target_preflight
            supplied_doc = _get_concept(supplied_paper_concept_id)
            if not _relation_contains_target(
                supplied_doc,
                "is_an_instance_of",
                "#V#scholarly_article",
            ):
                return WorkflowActionResult(
                    status="failed",
                    outputs={"paper_concept_id": supplied_paper_concept_id},
                    error="scholarly_paper_supplied_target_type_invalid",
                )
            return WorkflowActionResult(
                status="success",
                outputs={
                    "arxiv_id": _extract_arxiv_id_from_request(request),
                    "paper_instance_scope_mode": paper_scope_mode,
                    "paper_external_identity_present": False,
                    "paper_external_identity_scheme": None,
                    "paper_external_identity_value": None,
                    "paper_external_identity_concept_id": None,
                    "paper_external_identity_resolution_status": "supplied",
                    "paper_concept_id": supplied_paper_concept_id,
                },
            )

        metadata = _extract_metadata_from_context(request)
        source_uri = _first_non_empty_text(
            request.inputs.get("source_uri"),
            request.data.get("source_uri"),
            metadata.get("source_uri"),
            metadata.get("source_url"),
            metadata.get("url"),
        )
        arxiv_id = _extract_arxiv_id_from_request(request)
        doi_candidates = _extract_doi_candidates(
            (
                request.inputs.get("doi"),
                request.data.get("doi"),
                source_uri,
            )
        ) or _extract_doi_candidates(metadata)
        doi = doi_candidates[0].casefold() if doi_candidates else ""
        title = _extract_title_from_request(request)
        publication_date = _extract_publication_date_from_request(request)
        author_names = _extract_author_names_from_request(request)
        bibliographic_identity_value: str | None = None
        if title and publication_date and author_names:
            bibliographic_material = {
                "authors": sorted(
                    {
                        " ".join(author_name.split()).casefold()
                        for author_name in author_names
                        if _clean_text(author_name)
                    }
                ),
                "publication_date": " ".join(publication_date.split()).casefold(),
                "title": " ".join(title.split()).casefold(),
            }
            if bibliographic_material["authors"]:
                bibliographic_digest = hashlib.sha256(
                    json.dumps(
                        bibliographic_material,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                bibliographic_identity_value = f"sha256:{bibliographic_digest}"

        identity_scheme: str | None = None
        identity_value: str | None = None
        if arxiv_id:
            identity_scheme = "arxiv"
            identity_value = arxiv_id
        elif doi:
            identity_scheme = "doi"
            identity_value = doi
        elif source_uri:
            identity_scheme = "url"
            identity_value = source_uri
        elif bibliographic_identity_value:
            identity_scheme = "bibliographic"
            identity_value = bibliographic_identity_value

        identity_concept_id: str | None = None
        identity_resolution_status = "not_applicable"
        resolved_paper_concept_id: str | None = None
        if identity_scheme and identity_value:
            from ...services.concept_external_identity_service import (
                ExternalIdentifier,
                canonical_concept_id_for_external_identifiers,
            )

            identity_concept_id = canonical_concept_id_for_external_identifiers(
                [
                    ExternalIdentifier(
                        scheme=identity_scheme,
                        value=identity_value,
                    )
                ],
                kind="instance",
                parent_id="#V#scholarly_article",
                scope_mode=paper_scope_mode,
                actor_user_id=_trusted_actor_concept_id(request),
                actor_org_id=_clean_text(
                    getattr(request.environment, "org_concept_id", None)
                )
                or None,
            )
            if not identity_concept_id:
                return WorkflowActionResult(
                    status="failed",
                    error="scholarly_paper_external_identity_invalid",
                )
            existing_doc = _get_concept(identity_concept_id)
            if existing_doc is None:
                identity_resolution_status = "not_found"
            else:
                target_preflight = _require_global_targets(
                    (identity_concept_id,),
                    error_code="scholarly_paper_global_target_required",
                )
                if target_preflight is not None:
                    return target_preflight
                if not _relation_contains_target(
                    existing_doc,
                    "is_an_instance_of",
                    "#V#scholarly_article",
                ):
                    return WorkflowActionResult(
                        status="failed",
                        outputs={
                            "paper_external_identity_concept_id": identity_concept_id,
                        },
                        error="scholarly_paper_external_identity_conflict",
                    )
                identity_resolution_status = "resolved"
                resolved_paper_concept_id = identity_concept_id

        return WorkflowActionResult(
            status="success",
            outputs={
                "arxiv_id": arxiv_id,
                "paper_instance_scope_mode": paper_scope_mode,
                "paper_external_identity_present": bool(identity_value),
                "paper_external_identity_scheme": identity_scheme,
                "paper_external_identity_value": identity_value,
                "paper_external_identity_concept_id": identity_concept_id,
                "paper_external_identity_resolution_status": (
                    identity_resolution_status
                ),
                "paper_concept_id": resolved_paper_concept_id,
            },
        )

    return _handle


def _build_materialise_from_file_copy_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        from ...services.arxiv_paper_link_service import (
            _stable_file_copy_paper_instance_concept_id,
            resolve_actor_private_arxiv_paper_concept_id,
        )

        user_concept_id = _trusted_actor_concept_id(request)
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
        preflight_targets = [file_copy_concept_id]
        if arxiv_id:
            expected_paper_concept_id = resolve_actor_private_arxiv_paper_concept_id(
                user_concept_id=user_concept_id,
                arxiv_id=arxiv_id,
            )
            schema_preflight = _require_preprovisioned_schema_support(
                type_concept_ids=(
                    "#V#paper_on_arxiv",
                    "#V#scholarly_article",
                    "#V#person",
                    "#V#research_topic",
                ),
                predicate_concept_ids=("#V#authored_by", "#V#about"),
            )
            author_names = _extract_author_names_from_request(request)
            topic_labels = _extract_topic_labels_from_request(request)
            related_candidate_ids = [
                *(
                    predict_scholarly_author_concept_id(
                        user_concept_id=user_concept_id,
                        author_name=author_name,
                    )
                    for author_name in author_names
                ),
                *(
                    predict_scholarly_topic_concept_id(
                        user_concept_id=user_concept_id,
                        topic_label=topic_label,
                    )
                    for topic_label in topic_labels[:_MAX_TOPIC_RELATIONS]
                ),
            ]
            preflight_targets.extend(
                concept_id
                for concept_id in (expected_paper_concept_id, *related_candidate_ids)
                if _concept_exists(concept_id)
            )
        else:
            expected_paper_concept_id = _stable_file_copy_paper_instance_concept_id(
                file_copy_concept_id
            )
            schema_preflight = _require_preprovisioned_schema_support(
                type_concept_ids=("#V#scholarly_article",),
            )
            if _concept_exists(expected_paper_concept_id):
                preflight_targets.append(expected_paper_concept_id)

        target_preflight = _require_actor_private_targets(request, preflight_targets)
        if target_preflight is not None:
            return target_preflight
        if schema_preflight is not None:
            return schema_preflight

        if arxiv_id:
            report = materialise_scholarly_representation_for_arxiv_file_copy(
                user_concept_id=user_concept_id,
                arxiv_id=arxiv_id,
                file_copy_concept_id=file_copy_concept_id,
                metadata=metadata or None,
                logger=logger,
                schema_support_preprovisioned=True,
            )
        else:
            report = materialise_scholarly_representation_for_file_copy(
                user_concept_id=user_concept_id,
                file_copy_concept_id=file_copy_concept_id,
                metadata=metadata or None,
                logger=logger,
                schema_support_preprovisioned=True,
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
        target_preflight = _require_global_targets(
            (paper_concept_id,),
            error_code="scholarly_paper_global_target_required",
        )
        if target_preflight is not None:
            return target_preflight

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
                context={
                    "name_type": "CODE",
                    "source": "scholarly_workflow_enrichment",
                },
            )
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                text=f"https://arxiv.org/abs/{arxiv_id}",
                lang="en-NZ",
                context={
                    "name_type": "CODE",
                    "source": "scholarly_workflow_enrichment",
                },
            )
            applied = True

        if source_uri:
            upsert_text_for_concept(
                subject_concept_id=paper_concept_id,
                predicate="hasName",
                text=source_uri,
                lang="en-NZ",
                context={
                    "name_type": "CODE",
                    "source": "scholarly_workflow_enrichment",
                },
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

        schema_preflight = _require_preprovisioned_schema_support(
            type_concept_ids=("#V#person",),
            predicate_concept_ids=("#V#authored_by",),
        )
        if schema_preflight is not None:
            return schema_preflight
        target_preflight = _require_global_targets(
            (paper_concept_id,),
            error_code="scholarly_paper_global_target_required",
        )
        if target_preflight is not None:
            return target_preflight

        instance_profile = resolve_publication_scope_profile(
            plane="instance",
            type_concept_ids=["#V#person"],
            source_kind="public_scholarly_metadata",
            stable_identity_present=True,
            producer="#V#scholarly_article_metadata_representation_workflow",
        )
        assertion_profile = resolve_publication_scope_profile(
            plane="assertion",
            predicate_concept_id="#V#authored_by",
            subject_type_concept_ids=["#V#scholarly_article"],
            object_type_concept_ids=["#V#person"],
            source_kind="public_scholarly_metadata",
            stable_identity_present=True,
            producer="#V#scholarly_article_metadata_representation_workflow",
        )
        if (
            not instance_profile.get("success")
            or instance_profile.get("selected_scope_mode") != "global_general"
            or not assertion_profile.get("success")
            or assertion_profile.get("selected_scope_mode") != "global_general"
        ):
            return WorkflowActionResult(
                status="failed",
                outputs={
                    "author_instance_publication_profile": instance_profile,
                    "authorship_assertion_publication_profile": assertion_profile,
                },
                error="scholarly_author_global_scope_profile_required",
            )

        try:
            author_records = _public_author_records_for_paper(
                request,
                paper_concept_id=paper_concept_id,
            )
        except ValueError as exc:
            return WorkflowActionResult(status="failed", error=str(exc))

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "author_names": [record["name"] for record in author_records],
                "public_author_records": author_records,
                "public_author_count": len(author_records),
                "author_instance_scope_mode": instance_profile.get(
                    "selected_scope_mode"
                ),
                "authorship_assertion_scope_mode": assertion_profile.get(
                    "selected_scope_mode"
                ),
                "author_scope_profiles_resolved": True,
            },
        )

    return _handle


def _has_non_code_title(
    name_rows: list[Mapping[str, Any]], *, arxiv_id: str | None
) -> bool:
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
        require_file_copy = _coerce_bool(
            request.inputs.get("require_file_copy"),
            default=_coerce_bool(request.data.get("require_file_copy"), default=True),
        )
        verification_profile = _resolve_verification_profile(request)
        arxiv_id = _extract_arxiv_id_from_request(request)
        expected_publication_date = _extract_publication_date_from_request(request)

        verification_failures: list[str] = []
        paper_doc = _get_concept(paper_concept_id or "")
        if not paper_doc:
            verification_failures.append("paper_concept_missing")
        paper_publication_context: dict[str, Any] | None = None
        if paper_concept_id and paper_doc:
            try:
                paper_context = concept_publication_context(paper_concept_id)
                paper_publication_context = paper_context.to_mapping()
                if paper_context.kind != PublicationContextKind.GLOBAL:
                    verification_failures.append("paper_not_global")
            except (LookupError, ValueError):
                verification_failures.append("paper_publication_context_missing")

        if require_file_copy and not file_copy_concept_id:
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
        file_link_assertion_id: str | None = None
        canonical_file_link_present = False
        if file_copy_concept_id:
            canonical_file_link_present = _relation_contains_target(
                paper_doc,
                "#V#propositional_information_thing_has_computer_file",
                file_copy_concept_id,
            )
            scoped_file_links = _visible_local_file_copy_assertions(
                request,
                paper_concept_id=paper_concept_id or "",
            )
            matching_file_links = [
                assertion
                for assertion in scoped_file_links
                if _clean_text(assertion.get("object_concept_id"))
                == file_copy_concept_id
                and assertion.get("canonical_publication") is False
            ]
            file_link_verified = bool(matching_file_links)
            if matching_file_links:
                file_link_assertion_id = (
                    _clean_text(matching_file_links[0].get("assertion_id")) or None
                )
            if canonical_file_link_present:
                verification_failures.append("file_link_scope_leak")
        if file_copy_concept_id and not file_link_verified:
            verification_failures.append("file_link_missing")

        has_name_signal = bool(name_rows) or bool(
            _clean_text((paper_doc or {}).get("name"))
        )
        if not has_name_signal:
            verification_failures.append("title_or_name_missing")

        author_concept_ids = _relationship_targets(paper_doc, "#V#authored_by")
        non_global_author_concept_ids: list[str] = []
        for author_concept_id in author_concept_ids:
            try:
                author_context = concept_publication_context(author_concept_id)
            except (LookupError, ValueError):
                non_global_author_concept_ids.append(author_concept_id)
                continue
            if author_context.kind != PublicationContextKind.GLOBAL:
                non_global_author_concept_ids.append(author_concept_id)
        if non_global_author_concept_ids:
            verification_failures.append("authors_not_global")
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
                paper_doc.get("attributes") if isinstance(paper_doc, Mapping) else None
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
                "require_file_copy": require_file_copy,
                "scholarly_representation_verified": verified,
                "verification_failures": verification_failures,
                "author_concept_ids": author_concept_ids,
                "topic_concept_ids": topic_concept_ids,
                "publication_date": (
                    _clean_text(publication_date_rows[0].get("text"))
                    if publication_date_rows
                    and isinstance(publication_date_rows[0], Mapping)
                    else None
                ),
                "type_asserted": type_asserted,
                "file_link_verified": file_link_verified,
                "file_link_assertion_id": file_link_assertion_id,
                "file_link_canonical_publication": False,
                "canonical_file_link_present": canonical_file_link_present,
                "paper_publication_context": paper_publication_context,
                "non_global_author_concept_ids": non_global_author_concept_ids,
            },
        )

    return _handle


def _build_scholarly_paper_ensure_paper_concept_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        from ...services.arxiv_paper_link_service import (
            _stable_file_copy_paper_instance_concept_id,
            ensure_arxiv_paper_instance,
            resolve_actor_private_arxiv_paper_concept_id,
        )

        user_concept_id = _trusted_actor_concept_id(request)
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

        if arxiv_id:
            paper_concept_id = resolve_actor_private_arxiv_paper_concept_id(
                user_concept_id=user_concept_id,
                arxiv_id=arxiv_id,
            )
            schema_preflight = _require_preprovisioned_schema_support(
                type_concept_ids=("#V#paper_on_arxiv", "#V#scholarly_article"),
            )
            existed_before = _concept_exists(paper_concept_id)
            preflight_targets = [paper_concept_id] if existed_before else []
        elif file_copy_concept_id:
            paper_concept_id = _stable_file_copy_paper_instance_concept_id(
                file_copy_concept_id
            )
            schema_preflight = _require_preprovisioned_schema_support(
                type_concept_ids=("#V#scholarly_article",),
            )
            existed_before = _concept_exists(paper_concept_id)
            preflight_targets = [file_copy_concept_id]
            if existed_before:
                preflight_targets.append(paper_concept_id)
        else:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_insufficient_identifiers",
            )

        target_preflight = _require_actor_private_targets(request, preflight_targets)
        if target_preflight is not None:
            return target_preflight
        if schema_preflight is not None:
            return schema_preflight

        created = not existed_before
        if arxiv_id:
            paper_concept_id = ensure_arxiv_paper_instance(
                user_concept_id=user_concept_id,
                arxiv_id=arxiv_id,
                logger=logger,
                schema_support_preprovisioned=True,
            )
        elif created:
            if file_copy_concept_id:
                metadata = _extract_metadata_from_context(request)
                title = extract_scholarly_metadata_title(metadata)
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
                    created_by_concept_id=user_concept_id,
                    visibility_scope_mode="user_only_default",
                    maintain_relationship_inverses=False,
                    resolve_visibility_from_event_namespace=False,
                )

        # The source was actor-private-preflighted above. Suppress the inverse
        # so this private effect does not also mutate the global schema type.
        type_relation = add_structural_relationship(
            source_id=paper_concept_id,
            predicate="is_an_instance_of",
            target_id="#V#scholarly_article",
            maintain_inverse=False,
        )
        if not bool(type_relation.get("success")):
            return WorkflowActionResult(
                status="failed",
                outputs={
                    "paper_concept_id": paper_concept_id,
                    "type_relationship_write_result": type_relation,
                },
                error=(
                    "scholarly_paper_type_assertion_failed:"
                    f"{type_relation.get('error') or 'unknown_error'}"
                ),
            )

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "paper_concept_created": created,
                "type_asserted": bool(type_relation.get("success")),
            },
        )

    return _handle


def _build_scholarly_paper_link_file_copy_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
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
        target_preflight = _require_global_targets(
            (paper_concept_id,),
            error_code="scholarly_paper_global_target_required",
        )
        if target_preflight is not None:
            return target_preflight
        actor_concept_id = _trusted_actor_concept_id(request)
        if not actor_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="scholarly_paper_actor_user_missing",
            )
        scope_profile = resolve_publication_scope_profile(
            plane="assertion",
            predicate_concept_id=_LOCAL_FILE_COPY_PREDICATE_ID,
            subject_type_concept_ids=["#V#scholarly_article"],
            object_type_concept_ids=["#V#computer_file_copy"],
            source_kind="private_email",
            producer="#V#scholarly_article_metadata_representation_workflow",
        )
        carrier = scope_profile.get("carrier")
        carrier_scope_mode = (
            _clean_text(carrier.get("scope_mode"))
            if isinstance(carrier, Mapping)
            else ""
        )
        if (
            not scope_profile.get("success")
            or scope_profile.get("selected_scope_mode") != "user_only_default"
            or carrier_scope_mode != "user"
        ):
            return WorkflowActionResult(
                status="failed",
                outputs={"file_link_publication_profile": scope_profile},
                error="scholarly_paper_file_link_scoped_profile_required",
            )
        try:
            receipt = upsert_scoped_assertion(
                subject_concept_id=paper_concept_id,
                predicate=_LOCAL_FILE_COPY_PREDICATE_ID,
                target_concept_id=file_copy_concept_id,
                scope_mode=carrier_scope_mode,
                acting_user_concept_id=actor_concept_id,
                organisation_concept_id=(
                    _clean_text(getattr(request.environment, "org_concept_id", None))
                    or None
                ),
                namespace=(
                    _clean_text(getattr(request.environment, "user_namespace", None))
                    or None
                ),
                evidence={
                    "source_kind": "private_email",
                    "workflow_id": (
                        request.workflow_id
                        or "#V#scholarly_article_metadata_representation_workflow"
                    ),
                },
                canonical_publication=False,
            )
        except (PermissionError, RuntimeError, ValueError) as exc:
            return WorkflowActionResult(
                status="failed",
                error=f"scholarly_paper_file_link_assertion_failed:{exc}",
            )

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "file_copy_concept_id": file_copy_concept_id,
                "file_link_written": True,
                "file_link_changed": bool(receipt.get("changed")),
                "file_link_assertion_id": receipt.get("assertion_id"),
                "file_link_scope_mode": carrier_scope_mode,
                "file_link_canonical_publication": False,
                "file_link_canonical_read_back": receipt.get("canonical_read_back"),
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

        target_preflight = _require_global_targets(
            (paper_concept_id,),
            error_code="scholarly_paper_global_target_required",
        )
        if target_preflight is not None:
            return target_preflight

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
        target_preflight = _require_global_targets(
            (paper_concept_id,),
            error_code="scholarly_paper_global_target_required",
        )
        if target_preflight is not None:
            return target_preflight

        return WorkflowActionResult(
            status="success",
            outputs={
                "paper_concept_id": paper_concept_id,
                "topic_labels": topic_labels,
                "topic_labels_text": ", ".join(topic_labels),
                "topic_labels_present": bool(topic_labels),
                "topic_normalisation_completed": True,
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
        target_preflight = _require_global_targets(
            (paper_concept_id,),
            error_code="scholarly_paper_global_target_required",
        )
        if target_preflight is not None:
            return target_preflight

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


def _build_prepare_public_title_search_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        metadata = _extract_metadata_from_context(request)
        doi_candidates = _extract_doi_candidates(
            (
                request.inputs.get("doi"),
                request.data.get("doi"),
                metadata.get("doi"),
            )
        )
        stable_source_uri = _first_non_empty_text(
            request.inputs.get("source_uri"),
            request.data.get("source_uri"),
            metadata.get("source_uri"),
            metadata.get("source_url"),
        )
        title = _first_non_empty_text(
            request.inputs.get("title"),
            request.data.get("title"),
            metadata.get("title"),
            metadata.get("paper_title"),
        )
        publication_date = _first_non_empty_text(
            request.inputs.get("publication_date"),
            request.data.get("publication_date"),
            metadata.get("publication_date"),
            metadata.get("published"),
        )
        author_names = _coerce_string_list(
            request.inputs.get("author_names")
            or request.data.get("author_names")
            or metadata.get("authors")
        )

        bibliographic_fallback_basis: str | None = None
        if doi_candidates:
            bibliographic_fallback_basis = "doi"
        elif stable_source_uri:
            bibliographic_fallback_basis = "source_uri"
        elif title and publication_date and author_names:
            bibliographic_fallback_basis = "title_publication_date_authors"

        if doi_candidates or stable_source_uri:
            return WorkflowActionResult(
                status="success",
                outputs={
                    "paper_metadata": metadata or None,
                    "public_paper_title": title,
                    "public_paper_title_query": title,
                    "public_paper_title_search_required": False,
                    "bibliographic_fallback_sufficient": True,
                    "bibliographic_fallback_basis": bibliographic_fallback_basis,
                },
            )
        if not title:
            return WorkflowActionResult(
                status="failed",
                outputs={
                    "paper_metadata": metadata or None,
                    "public_paper_title_search_required": False,
                    "bibliographic_fallback_sufficient": False,
                    "bibliographic_fallback_basis": "insufficient",
                },
                error="public_paper_title_missing",
            )
        return WorkflowActionResult(
            status="success",
            outputs={
                "public_paper_title": title,
                "public_paper_title_query": title,
                "paper_metadata": metadata or {"title": title},
                "public_paper_title_search_required": True,
                "bibliographic_fallback_sufficient": bool(bibliographic_fallback_basis),
                "bibliographic_fallback_basis": (
                    bibliographic_fallback_basis or "insufficient"
                ),
            },
        )

    return _handle


def _build_select_arxiv_title_match_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        metadata = _extract_metadata_from_context(request)
        requested_title = _first_non_empty_text(
            request.inputs.get("title"),
            request.data.get("public_paper_title"),
            request.data.get("title"),
            metadata.get("title"),
            metadata.get("paper_title"),
        )
        title_key = _normalise_public_title(requested_title)
        if not title_key:
            return WorkflowActionResult(
                status="failed",
                error="public_paper_title_missing",
            )

        payload = (
            request.inputs.get("search_payload")
            or request.inputs.get("public_paper_search_payload")
            or request.data.get("public_paper_search_payload")
            or request.data.get("search_payload")
        )
        rows = _public_search_result_rows(payload)
        exact_rows = [
            row
            for row in rows
            if _normalise_public_title(
                _first_non_empty_text(row.get("title"), row.get("paper_title"))
            )
            == title_key
        ]

        requested_year = (
            _first_non_empty_text(
                request.inputs.get("publication_date"),
                request.data.get("publication_date"),
                metadata.get("publication_date"),
                metadata.get("published"),
            )
            or ""
        )[:4]
        if len(exact_rows) > 1 and requested_year.isdigit():
            year_rows = [
                row
                for row in exact_rows
                if _first_non_empty_text(
                    row.get("published"),
                    row.get("publication_date"),
                    row.get("updated"),
                )[:4]
                == requested_year
            ]
            if year_rows:
                exact_rows = year_rows

        requested_authors = {
            _normalise_public_title(author)
            for author in _coerce_string_list(
                request.inputs.get("author_names")
                or request.data.get("author_names")
                or metadata.get("authors")
            )
            if _normalise_public_title(author)
        }
        if len(exact_rows) > 1 and requested_authors:
            scored_rows = [
                (
                    len(
                        requested_authors
                        & {
                            _normalise_public_title(author)
                            for author in _coerce_string_list(row.get("authors"))
                            if _normalise_public_title(author)
                        }
                    ),
                    row,
                )
                for row in exact_rows
            ]
            best_score = max(score for score, _row in scored_rows)
            if best_score > 0:
                exact_rows = [
                    row for score, row in scored_rows if score == best_score
                ]

        if len(exact_rows) != 1:
            error_code = (
                "public_paper_title_match_not_found"
                if not exact_rows
                else "public_paper_title_match_ambiguous"
            )
            return WorkflowActionResult(
                status="failed",
                outputs={
                    "public_paper_resolution_status": error_code,
                    "public_paper_search_result_count": len(rows),
                    "public_paper_exact_match_count": len(exact_rows),
                },
                error=error_code,
            )

        matched = exact_rows[0]
        identity_candidates = extract_arxiv_id_candidates(
            [
                matched.get("arxiv_id"),
                matched.get("id"),
                matched.get("paper_id"),
                matched.get("entry_id"),
                matched.get("url"),
            ]
        )
        if not identity_candidates:
            return WorkflowActionResult(
                status="failed",
                outputs={
                    "public_paper_resolution_status": (
                        "public_paper_exact_match_identity_missing"
                    ),
                    "public_paper_search_result_count": len(rows),
                    "public_paper_exact_match_count": 1,
                },
                error="public_paper_exact_match_identity_missing",
            )

        arxiv_id = identity_candidates[0]
        resolved_metadata = {
            key: value
            for key, value in {
                **metadata,
                "title": _first_non_empty_text(
                    matched.get("title"), requested_title
                ),
                "authors": _coerce_string_list(matched.get("authors"))
                or _coerce_string_list(metadata.get("authors")),
                "abstract": _first_non_empty_text(
                    matched.get("summary"),
                    matched.get("abstract"),
                    metadata.get("abstract"),
                    metadata.get("summary"),
                ),
                "publication_date": _first_non_empty_text(
                    matched.get("published"),
                    matched.get("publication_date"),
                    metadata.get("publication_date"),
                ),
                "categories": _coerce_string_list(
                    matched.get("categories") or metadata.get("categories")
                ),
                "source_uri": f"https://arxiv.org/abs/{arxiv_id}",
                "arxiv_id": arxiv_id,
            }.items()
            if value not in (None, "", [], {})
        }
        return WorkflowActionResult(
            status="success",
            outputs={
                "arxiv_id": arxiv_id,
                "source_uri": f"https://arxiv.org/abs/{arxiv_id}",
                "paper_metadata": resolved_metadata,
                "public_paper_resolution_status": "exact_public_title_match",
                "public_paper_search_result_count": len(rows),
                "public_paper_exact_match_count": 1,
                "public_paper_matched_result": matched,
            },
        )

    return _handle


def _build_paper_reference_normalise_set_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        source_context = _normalise_source_context(
            request.inputs.get("source_context")
            or request.data.get("source_context")
            or {},
            provenance_required=True,
        )
        preview_only = _coerce_bool(
            request.inputs.get("preview_only") or request.data.get("preview_only"),
            default=False,
        )
        candidate_items = _build_reference_item_candidates(request)
        normalised_items = [
            _normalise_paper_reference_item(
                item,
                source_context=source_context,
                default_preview_only=preview_only,
                index=index,
            )
            for index, item in enumerate(candidate_items)
        ]
        normalised_items = _dedupe_reference_items(normalised_items)
        if not normalised_items:
            return WorkflowActionResult(
                status="failed",
                error="paper_reference_set_missing",
                outputs={
                    "paper_reference_error_code": "paper_reference_set_missing",
                    "paper_reference_error_message": (
                        "No paper references were supplied in the launch inputs "
                        "or shared workflow context."
                    ),
                    "paper_reference_items": [],
                    "paper_reference_item_count": 0,
                    "paper_reference_preview_only": preview_only,
                    "normalised_paper_reference_set": {
                        "schema_version": "paper_reference_set.v1",
                        "items": [],
                        "source_context": source_context,
                    },
                },
            )

        reference_set = {
            "schema_version": "paper_reference_set.v1",
            "items": normalised_items,
            "source_context": source_context,
        }
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": reference_set,
                "normalised_paper_reference_set": reference_set,
                "paper_reference_items": normalised_items,
                "paper_reference_item_count": len(normalised_items),
                "paper_reference_preview_only": preview_only,
                "paper_reference_kinds": [
                    item.get("reference_kind") for item in normalised_items
                ],
                "paper_reference_error_code": None,
            },
        )

    return _handle


def _build_paper_reference_fail_item_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        error_code = _first_non_empty_text(
            request.inputs.get("error_code"),
            request.data.get("paper_reference_error_code"),
            "paper_reference_ingestion_failed",
        )
        error_message = _first_non_empty_text(
            request.inputs.get("error_message"),
            request.data.get("paper_reference_error_message"),
            error_code,
        )
        outputs = {
            "paper_reference_item_status": "failed",
            "paper_reference_error_code": error_code,
            "paper_reference_error_message": error_message,
            "paper_reference_kind": _first_non_empty_text(
                request.inputs.get("reference_kind"),
                request.data.get("paper_reference_kind"),
                "unknown",
            ),
            "paper_reference_index": request.data.get("paper_reference_index"),
        }
        return WorkflowActionResult(
            status="failed",
            error=error_code,
            outputs=outputs,
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

        from ...services.concept_external_identity_service import (
            ExternalIdentifier,
            canonical_concept_id_for_external_identifiers,
        )

        predicted_cid = canonical_concept_id_for_external_identifiers(
            (
                ExternalIdentifier(
                    scheme="arxiv",
                    value=arxiv_id,
                    source="public_scholarly_metadata",
                ),
            ),
            kind="instance",
            parent_id="#V#scholarly_article",
            scope_mode="global_general",
        )

        paper_concept_id = None
        if predicted_cid and _concept_exists(predicted_cid):
            paper_concept_id = predicted_cid

        file_copy_concept_id = None
        if paper_concept_id:
            targets = [
                _clean_text(assertion.get("object_concept_id"))
                for assertion in _visible_local_file_copy_assertions(
                    request,
                    paper_concept_id=paper_concept_id,
                )
            ]
            targets = [target for target in targets if target]
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
            metadata_status="fetched"
            if _extract_metadata_from_context(request)
            else "missing",
            acquisition_mode=_first_non_empty_text(
                request.inputs.get("acquisition_mode"),
                request.data.get("acquisition_mode"),
            ),
            download_attempted=bool(
                request.inputs.get("download_attempted")
                or request.data.get("download_attempted")
            ),
            download_succeeded=bool(
                request.inputs.get("download_succeeded")
                or request.data.get("download_succeeded")
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
                request.inputs.get("author_concept_ids")
                or request.data.get("author_concept_ids")
            ),
            topic_concept_ids=_coerce_string_list(
                request.inputs.get("topic_concept_ids")
                or request.data.get("topic_concept_ids")
            ),
            verification_status="verified"
            if verified
            else "failed"
            if verification_failures
            else "unknown",
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
            action_id=SCHOLARLY_PAPER_NORMALISE_EXTERNAL_IDENTITY_ACTION_ID,
            handler=_build_normalise_external_identity_handler(),
            description=(
                "Normalise a paper's arXiv, DOI, or source-URI identity before "
                "title-based resolution."
            ),
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
            description=(
                "Link a public paper to a local file copy through an actor-scoped "
                "assertion."
            ),
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
            description=(
                "Prepare source-bounded or publicly identified author records "
                "without mutating concepts."
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=SCHOLARLY_PAPER_RESOLVE_TOPICS_ACTION_ID,
            handler=_build_scholarly_paper_resolve_topics_handler(),
            description=(
                "Normalise public topic labels without minting name-only topic "
                "concepts."
            ),
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
            required_tool_operation_class="verification_read",
            required_tool_target_argument_names=(
                "arxiv_id",
                "paper_concept_id",
                "file_copy_concept_id",
            ),
            required_tool_target_payload_field_names=(
                "arxiv_id",
                "paper_concept_id",
                "file_copy_concept_id",
            ),
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
    registry.register_if_absent(
        ActionSpec(
            action_id=PAPER_REFERENCE_NORMALISE_SET_ACTION_ID,
            handler=_build_paper_reference_normalise_set_handler(),
            description=(
                "Normalise caller-supplied scholarly paper references into a "
                "source-neutral reference-set schema for represented workflow fan-out."
            ),
            required_tool_operation_class="search_or_resolution_read",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=PAPER_REFERENCE_PREPARE_PUBLIC_TITLE_SEARCH_ACTION_ID,
            handler=_build_prepare_public_title_search_handler(),
            description=(
                "Prepare an exact public-title search for an otherwise "
                "unidentified scholarly paper reference."
            ),
            required_tool_operation_class="search_or_resolution_read",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=PAPER_REFERENCE_SELECT_ARXIV_TITLE_MATCH_ACTION_ID,
            handler=_build_select_arxiv_title_match_handler(),
            description=(
                "Select one exact public arXiv title match, failing closed on "
                "absence, ambiguity, or missing public identity."
            ),
            required_tool_operation_class="search_or_resolution_read",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=PAPER_REFERENCE_FAIL_ITEM_ACTION_ID,
            handler=_build_paper_reference_fail_item_handler(),
            description="Fail a source-neutral paper reference item with typed outputs.",
        )
    )


__all__ = [
    "ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID",
    "ARXIV_NORMALISE_SOURCE_ACTION_ID",
    "PAPER_REFERENCE_FAIL_ITEM_ACTION_ID",
    "PAPER_REFERENCE_NORMALISE_SET_ACTION_ID",
    "SCHOLARLY_PAPER_ENRICH_ACTION_ID",
    "SCHOLARLY_PAPER_MATERIALISE_ACTION_ID",
    "SCHOLARLY_PAPER_NORMALISE_EXTERNAL_IDENTITY_ACTION_ID",
    "SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID",
    "SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID",
    "SCHOLARLY_PAPER_VERIFY_ACTION_ID",
    "register_paper_representation_actions",
]
