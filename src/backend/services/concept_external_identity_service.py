"""Bounded external-identity support for concept creation.

The create-concepts path normally derives a concept ID from the requested
display name.  That is not sufficient for entities such as scholarly papers,
whose title can vary while an external identifier remains stable.  This module
provides mechanical identifier normalisation, actor-scoped candidate lookup,
and identity-name persistence without making merge or reconciliation choices.

Only arXiv identity is supported initially.  Adding another scheme requires an
explicit normaliser and exact-verification rule; arbitrary description text is
never treated as an entity identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import filter_accessible_concept_ids
from .concept_search_service import (
    CONCEPT_SEARCH_QUERY_MAX_TIME_MS,
    _consume_search_cursor,
)
from .text_value_service import get_texts_for_concepts, upsert_text_for_concept

_ARXIV_ID_BODY = r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.\-]+/\d{7})"
_ARXIV_FULL_PATTERN = re.compile(
    rf"(?i)^(?:arxiv\s*:?\s*)?({_ARXIV_ID_BODY})(?:v\d+)?$"
)
_ARXIV_URL_PATTERN = re.compile(
    rf"(?i)^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/"
    rf"({_ARXIV_ID_BODY})(?:v\d+)?(?:\.pdf)?/?(?:[?#].*)?$"
)
_LEADING_ARXIV_LABEL_PATTERN = re.compile(
    rf"(?i)^\s*arxiv\s*:?\s*({_ARXIV_ID_BODY})(?:v\d+)?" r"(?=\s*(?:$|[-–—:|]))"
)
_SUPPORTED_SCHEMES = frozenset({"arxiv"})
_ARXIV_PAPER_PARENT_IDS = frozenset(
    {
        "#V#academic_paper",
        "#V#paper",
        "#V#paper_on_arxiv",
        "#V#publication",
        "#V#research_paper",
        "#V#scholarly_article",
        "#V#scholarly_work",
        "#V#scientific_paper",
    }
)
_ARXIV_PAPER_HIERARCHY_ROOT_IDS = frozenset(
    {
        "#V#academic_paper",
        "#V#paper",
        "#V#paper_on_arxiv",
        "#V#research_paper",
        "#V#scholarly_article",
        "#V#scientific_paper",
    }
)
_MAX_EXTERNAL_IDENTITY_CANDIDATES = 50
_MAX_EXTERNAL_IDENTITY_TEXT_VALUES = 200
_MAX_EXTERNAL_IDENTITY_TEXT_RELATIONS = 2_000
_MAX_IDENTITY_NAMES_PER_CONCEPT = 50
_MAX_PAPER_PARENT_ANCESTOR_DEPTH = 6


class ExternalIdentityInputError(ValueError):
    """Typed invalid external-identity input."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details or {})


@dataclass(frozen=True)
class ExternalIdentifier:
    scheme: str
    value: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {
            "scheme": self.scheme,
            "value": self.value,
            "source": self.source,
        }


@dataclass(frozen=True)
class ExternalIdentityResolution:
    status: str
    identifier: ExternalIdentifier
    candidate_concept_ids: tuple[str, ...] = ()


def normalise_arxiv_id(value: Any) -> str | None:
    """Return a lower-case, version-free arXiv identifier."""

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None

    url_match = _ARXIV_URL_PATTERN.fullmatch(cleaned)
    if url_match:
        return url_match.group(1).lower()

    full_match = _ARXIV_FULL_PATTERN.fullmatch(cleaned)
    if full_match:
        return full_match.group(1).lower()
    return None


def _leading_arxiv_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _LEADING_ARXIV_LABEL_PATTERN.search(value)
    return match.group(1).lower() if match else None


def _verified_arxiv_ids_from_name(value: Any) -> set[str]:
    """Extract only an exact ID/URL or a leading labelled paper identity."""

    if not isinstance(value, str):
        return set()
    exact = normalise_arxiv_id(value)
    if exact:
        return {exact}
    leading = _leading_arxiv_identifier(value)
    return {leading} if leading else set()


def _arxiv_identity_relation_candidates(arxiv_id: str) -> set[str]:
    """Return bounded hasName candidates from one indexed fingerprint lookup."""

    normalised_id = arxiv_id.casefold()
    url_prefixes = tuple(
        f"{scheme}://{host}arxiv.org/{path}/{normalised_id}"
        for scheme in ("http", "https")
        for host in ("", "www.")
        for path in ("abs", "pdf")
    )
    labelled_prefixes = (
        f"arxiv{normalised_id}",
        f"arxiv:{normalised_id}",
        f"arxiv: {normalised_id}",
        f"arxiv {normalised_id}",
        f"arxiv :{normalised_id}",
        f"arxiv : {normalised_id}",
    )
    # A prefix range keeps this one lookup on the fingerprint index while
    # surfacing version, .pdf, slash, query, and fragment suffixes. The ranges
    # deliberately admit longer near-prefixes; exact identity verification
    # below rejects those before any candidate can be reused.
    fingerprint_prefixes = (
        normalised_id,
        *url_prefixes,
        *labelled_prefixes,
    )
    matching_texts = _consume_search_cursor(
        TextValuesRepository.find(
            {
                "$or": [
                    {
                        "fingerprint": {
                            "$type": "string",
                            "$gte": prefix,
                            "$lt": f"{prefix}\uffff",
                        }
                    }
                    for prefix in fingerprint_prefixes
                ]
            },
            projection={"_id": 1},
            limit=_MAX_EXTERNAL_IDENTITY_TEXT_VALUES,
            max_time_ms=CONCEPT_SEARCH_QUERY_MAX_TIME_MS,
        )
    )
    text_value_ids: list[Any] = []
    for row in matching_texts:
        raw_id = row.get("_id") if isinstance(row, Mapping) else None
        if raw_id is None:
            continue
        text_value_ids.extend((raw_id, str(raw_id)))
    if not text_value_ids:
        return set()

    relations = _consume_search_cursor(
        TextRelationsRepository.find(
            {
                "object_text_id": {"$in": text_value_ids},
                "predicate": "hasName",
            },
            projection={"subject_concept_id": 1},
            limit=_MAX_EXTERNAL_IDENTITY_TEXT_RELATIONS,
            max_time_ms=CONCEPT_SEARCH_QUERY_MAX_TIME_MS,
        )
    )
    return {
        str(row.get("subject_concept_id")).strip()
        for row in relations
        if isinstance(row, Mapping)
        and isinstance(row.get("subject_concept_id"), str)
        and str(row.get("subject_concept_id")).strip()
    }


def _normalise_explicit_identifier(raw: Any) -> ExternalIdentifier:
    if isinstance(raw, ExternalIdentifier):
        return raw
    if not isinstance(raw, Mapping):
        raise ExternalIdentityInputError(
            "invalid_external_identifier",
            "Each external identifier must be an object.",
            details={"value_type": type(raw).__name__},
        )

    scheme = str(raw.get("scheme") or raw.get("type") or "").strip().lower()
    if not scheme:
        raise ExternalIdentityInputError(
            "invalid_external_identifier",
            "External identifier scheme is required.",
        )
    if scheme not in _SUPPORTED_SCHEMES:
        raise ExternalIdentityInputError(
            "unsupported_external_identifier_scheme",
            f"External identifier scheme '{scheme}' is not supported.",
            details={"scheme": scheme, "supported_schemes": sorted(_SUPPORTED_SCHEMES)},
        )

    role = str(raw.get("role") or "identity").strip().lower()
    if role != "identity":
        raise ExternalIdentityInputError(
            "invalid_external_identifier_role",
            "create_concepts external identifiers must have role='identity'.",
            details={"role": role},
        )

    value = normalise_arxiv_id(raw.get("value") or raw.get("identifier"))
    if not value:
        raise ExternalIdentityInputError(
            "invalid_external_identifier",
            "The supplied arXiv identifier is invalid.",
            details={"scheme": scheme},
        )
    return ExternalIdentifier(scheme=scheme, value=value, source="explicit")


def normalise_create_external_identifiers(
    *,
    external_identifiers: Any,
    concept_name: str,
    kind: str | None,
) -> tuple[ExternalIdentifier, ...]:
    """Normalise explicit identity input plus a conservative legacy name form."""

    explicit_items: list[Any] = []
    explicit_supplied = external_identifiers is not None
    if isinstance(external_identifiers, Mapping) or isinstance(
        external_identifiers, ExternalIdentifier
    ):
        explicit_items = [external_identifiers]
    elif isinstance(external_identifiers, Sequence) and not isinstance(
        external_identifiers, (str, bytes, bytearray)
    ):
        explicit_items = list(external_identifiers)
    elif explicit_supplied:
        raise ExternalIdentityInputError(
            "invalid_external_identifiers",
            "external_identifiers must be an object or list of objects.",
        )

    identifiers: list[ExternalIdentifier] = [
        _normalise_explicit_identifier(item) for item in explicit_items
    ]

    normalised_kind = str(kind or "type").strip().lower()
    if normalised_kind == "individual":
        normalised_kind = "instance"
    leading_arxiv_id = (
        _leading_arxiv_identifier(concept_name)
        if normalised_kind == "instance"
        else None
    )
    explicit_arxiv_ids = {item.value for item in identifiers if item.scheme == "arxiv"}
    if (
        leading_arxiv_id
        and explicit_arxiv_ids
        and leading_arxiv_id not in explicit_arxiv_ids
    ):
        raise ExternalIdentityInputError(
            "external_identifier_name_mismatch",
            "The leading arXiv label does not match external_identifiers.",
            details={
                "leading_arxiv_id": leading_arxiv_id,
                "explicit_arxiv_ids": sorted(explicit_arxiv_ids),
            },
        )
    if leading_arxiv_id and not explicit_arxiv_ids:
        identifiers.append(
            ExternalIdentifier(
                scheme="arxiv",
                value=leading_arxiv_id,
                source="leading_arxiv_label",
            )
        )

    deduplicated: list[ExternalIdentifier] = []
    seen: set[tuple[str, str]] = set()
    for identifier in identifiers:
        key = (identifier.scheme, identifier.value)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(identifier)

    if len(deduplicated) > 1:
        raise ExternalIdentityInputError(
            "conflicting_external_identifiers",
            "A create_concepts item must identify at most one external entity.",
            details={
                "external_identifiers": [item.to_dict() for item in deduplicated],
            },
        )
    return tuple(deduplicated)


def is_supported_arxiv_paper_parent(parent_id: str | None) -> bool:
    """Return whether a visible type is in the bounded scholarly-paper family."""

    from ..utils.concept_id_utils import canonicalise_vontology_concept_id

    canonical_parent_id = canonicalise_vontology_concept_id(parent_id)
    if canonical_parent_id in _ARXIV_PAPER_PARENT_IDS:
        return True
    if not canonical_parent_id:
        return False

    try:
        from .context_bundle_service import _collect_type_ancestors

        ancestors = _collect_type_ancestors(
            canonical_parent_id,
            max_depth=_MAX_PAPER_PARENT_ANCESTOR_DEPTH,
        )
    except Exception:
        return False
    ancestor_ids = {
        str(row.get("concept_id")).strip()
        for row in ancestors
        if isinstance(row, Mapping)
        and isinstance(row.get("concept_id"), str)
        and str(row.get("concept_id")).strip()
    }
    return bool(ancestor_ids.intersection(_ARXIV_PAPER_HIERARCHY_ROOT_IDS))


def arxiv_paper_parent_scopes_compatible(
    *,
    existing_parent_ids: Sequence[str],
    requested_parent_id: str | None,
) -> bool:
    """Treat supported scholarly-paper parent variants as one identity scope."""

    if not is_supported_arxiv_paper_parent(requested_parent_id):
        return False
    return any(
        is_supported_arxiv_paper_parent(parent_id) for parent_id in existing_parent_ids
    )


def _scope_stable_arxiv_concept_id(
    *,
    arxiv_id: str,
    scope_mode: str | None,
    actor_user_id: str | None,
    actor_org_id: str | None,
) -> str:
    from ..utils.concept_id_utils import canonicalise_vontology_concept_id
    from .arxiv_paper_link_service import predict_arxiv_paper_concept_id

    global_id = predict_arxiv_paper_concept_id(arxiv_id=arxiv_id)
    normalised_scope_mode = str(scope_mode or "").strip().lower()
    canonical_user_id = canonicalise_vontology_concept_id(actor_user_id)
    canonical_org_id = canonicalise_vontology_concept_id(actor_org_id)
    if normalised_scope_mode == "global_general" or not (
        canonical_user_id or canonical_org_id
    ):
        return global_id

    if canonical_org_id:
        visibility_scope = f"organisation:{canonical_org_id}"
    else:
        visibility_scope = f"user:{canonical_user_id}"
    scope_digest = hashlib.sha256(
        visibility_scope.casefold().encode("utf-8")
    ).hexdigest()[:10]
    return f"{global_id}_scope_{scope_digest}"


def resolve_external_identity_candidates(
    identifier: ExternalIdentifier,
    *,
    canonical_concept_id_candidates: Sequence[str] = (),
) -> ExternalIdentityResolution:
    """Resolve every actor-visible concept carrying an exact external identity."""

    if identifier.scheme != "arxiv":
        return ExternalIdentityResolution(status="not_found", identifier=identifier)

    from .arxiv_paper_link_service import predict_arxiv_paper_concept_id

    predicted_id = predict_arxiv_paper_concept_id(arxiv_id=identifier.value)
    trusted_predicted_ids = {predicted_id}
    scoped_id_pattern = re.compile(rf"^{re.escape(predicted_id)}_scope_[0-9a-f]{{10}}$")
    for candidate_id in canonical_concept_id_candidates:
        candidate_clean = candidate_id.strip() if isinstance(candidate_id, str) else ""
        if candidate_clean and scoped_id_pattern.fullmatch(candidate_clean):
            trusted_predicted_ids.add(candidate_clean)

    raw_candidate_ids: set[str] = set(trusted_predicted_ids)
    raw_candidate_ids.update(_arxiv_identity_relation_candidates(identifier.value))

    if not raw_candidate_ids:
        return ExternalIdentityResolution(status="not_found", identifier=identifier)

    existing_ids = {
        str(doc.get("concept_id")).strip()
        for doc in ConceptsRepository.find(
            {"concept_id": {"$in": sorted(raw_candidate_ids)}},
            projection={"concept_id": 1},
            limit=_MAX_EXTERNAL_IDENTITY_CANDIDATES,
        )
        if isinstance(doc, Mapping)
        and isinstance(doc.get("concept_id"), str)
        and str(doc.get("concept_id")).strip()
    }
    visible_ids = filter_accessible_concept_ids(existing_ids)
    if not visible_ids:
        return ExternalIdentityResolution(status="not_found", identifier=identifier)

    names_by_concept = get_texts_for_concepts(
        sorted(visible_ids),
        predicate="hasName",
        limit_per_concept=_MAX_IDENTITY_NAMES_PER_CONCEPT,
    )
    verified_ids: set[str] = set()
    for concept_id in visible_ids:
        if concept_id in trusted_predicted_ids:
            verified_ids.add(concept_id)
            continue
        for row in names_by_concept.get(concept_id, []):
            if identifier.value in _verified_arxiv_ids_from_name(row.get("text")):
                verified_ids.add(concept_id)
                break

    ordered_ids = tuple(sorted(verified_ids))
    if not ordered_ids:
        status = "not_found"
    elif len(ordered_ids) == 1:
        status = "resolved"
    else:
        status = "ambiguous"
    return ExternalIdentityResolution(
        status=status,
        identifier=identifier,
        candidate_concept_ids=ordered_ids,
    )


def canonical_concept_id_for_external_identifiers(
    identifiers: Sequence[ExternalIdentifier],
    *,
    kind: str,
    parent_id: str | None,
    scope_mode: str | None = None,
    actor_user_id: str | None = None,
    actor_org_id: str | None = None,
) -> str | None:
    """Return an atomic stable ID for a newly identified scholarly paper."""

    if len(identifiers) != 1:
        return None
    identifier = identifiers[0]
    normalised_kind = str(kind or "").strip().lower()
    if normalised_kind == "individual":
        normalised_kind = "instance"
    if normalised_kind != "instance" or identifier.scheme != "arxiv":
        return None

    if not is_supported_arxiv_paper_parent(parent_id):
        return None
    return _scope_stable_arxiv_concept_id(
        arxiv_id=identifier.value,
        scope_mode=scope_mode,
        actor_user_id=actor_user_id,
        actor_org_id=actor_org_id,
    )


def persist_external_identity_names(
    *,
    concept_id: str,
    identifiers: Sequence[ExternalIdentifier],
) -> dict[str, Any]:
    """Persist exact searchable identity names, retaining per-write failures."""

    writes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for identifier in identifiers:
        if identifier.scheme != "arxiv":
            continue
        for text in (
            identifier.value,
            f"https://arxiv.org/abs/{identifier.value}",
        ):
            try:
                result = upsert_text_for_concept(
                    subject_concept_id=concept_id,
                    predicate="hasName",
                    text=text,
                    lang="en-NZ",
                    provenance={
                        "source": "create_concepts_external_identity",
                        "external_identifier_scheme": identifier.scheme,
                    },
                    context={
                        "name_type": "CODE",
                        "source": "create_concepts_external_identity",
                    },
                )
                writes.append(
                    {
                        "scheme": identifier.scheme,
                        "value": text,
                        "relation_id": result.get("relation_id"),
                        "relation_created": bool(result.get("relation_created")),
                        "context_updated": bool(result.get("context_updated")),
                    }
                )
            except Exception as exc:
                failures.append(
                    {
                        "stage": "external_identity_persistence",
                        "scheme": identifier.scheme,
                        "value": text,
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                    }
                )
    return {
        "success": not failures,
        "writes": writes,
        "failures": failures,
    }


__all__ = [
    "ExternalIdentifier",
    "ExternalIdentityInputError",
    "ExternalIdentityResolution",
    "arxiv_paper_parent_scopes_compatible",
    "canonical_concept_id_for_external_identifiers",
    "is_supported_arxiv_paper_parent",
    "normalise_arxiv_id",
    "normalise_create_external_identifiers",
    "persist_external_identity_names",
    "resolve_external_identity_candidates",
]
