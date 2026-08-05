"""Generic external-identity support for concept creation.

The service treats an external identity as an explicit opaque pair:
``(scheme, canonical value)``.  It does not infer identities from display
names, interpret domain-specific identifier syntax, or decide which ontology
types are compatible.  Those semantic decisions remain with the caller,
represented workflow, or model that has the relevant evidence.

Code owns only the reusable mechanics:

* validate an explicit identity envelope;
* derive an actor-scoped stable create identity;
* locate exact persisted identity markers;
* surface legacy textual references as unverified candidates;
* verify caller-confirmed candidates or an exact reviewed non-match set; and
* persist a searchable identity marker without merging or retyping concepts.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import filter_accessible_concept_ids
from ..utils.concept_id_utils import canonicalise_vontology_concept_id
from .concept_search_service import _consume_search_cursor
from .text_value_service import get_texts_for_concepts, upsert_text_for_concept

_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]{0,63}$")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_MAX_EXTERNAL_IDENTIFIER_VALUE_CHARS = 512
_MAX_EXTERNAL_IDENTITY_CANDIDATES = 50
_MAX_EXTERNAL_IDENTITY_TEXT_VALUES = 200
_MAX_EXTERNAL_IDENTITY_TEXT_RELATIONS = 2_000
_MAX_IDENTITY_EVIDENCE_ROWS_PER_CONCEPT = 100
_MIN_LEGACY_DISCOVERY_VALUE_CHARS = 4
_IDENTITY_MARKER_PREFIX = "urn:von:external-identity:"
_IDENTITY_EVIDENCE_PREDICATES = (
    "hasName",
    "hasDescription",
    "hasContent",
    "hasNote",
    "#V#hasName",
    "#V#hasDescription",
    "#V#hasContent",
    "#V#hasNote",
)


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
    """One explicit canonical identifier supplied by an authorised caller."""

    scheme: str
    value: str
    source: str = "explicit"

    def to_dict(self) -> dict[str, str]:
        return {
            "scheme": self.scheme,
            "value": self.value,
            "source": self.source,
        }


@dataclass(frozen=True)
class ExternalIdentityResolution:
    """Bounded actor-visible identity lookup result."""

    status: str
    identifier: ExternalIdentifier
    candidate_concept_ids: tuple[str, ...] = ()
    resolution_source: str | None = None


@dataclass(frozen=True)
class ExternalIdentityCandidateScan:
    """Bounded candidate scan with enough finality to fail closed on overflow."""

    candidate_concept_ids: tuple[str, ...] = ()
    exhaustive: bool = True
    saturated_stages: tuple[str, ...] = ()


def _coerce_candidate_scan(
    value: ExternalIdentityCandidateScan | Sequence[str] | set[str],
) -> ExternalIdentityCandidateScan:
    """Keep private test seams compatible while production scans carry finality."""

    if isinstance(value, ExternalIdentityCandidateScan):
        return value
    return ExternalIdentityCandidateScan(
        candidate_concept_ids=tuple(
            sorted(
                {
                    item.strip()
                    for item in value
                    if isinstance(item, str) and item.strip()
                }
            )
        )
    )


def external_identity_marker(identifier: ExternalIdentifier) -> str:
    """Return the exact searchable marker for an opaque identity pair.

    Text-value fingerprints are case-insensitive, while canonical identifier
    values need not be. Hashing the structured JSON representation keeps
    case-distinct values distinct without putting an arbitrarily long opaque
    value into an index key.
    """

    digest = hashlib.sha256(_external_identity_material(identifier)).hexdigest()
    return f"{_IDENTITY_MARKER_PREFIX}sha256:{digest}"


def _external_identity_material(identifier: ExternalIdentifier) -> bytes:
    return json.dumps(
        {
            "scheme": identifier.scheme,
            "value": identifier.value,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalise_explicit_identifier(raw: Any) -> ExternalIdentifier:
    source = "explicit"
    if isinstance(raw, ExternalIdentifier):
        source = raw.source
        raw = {
            "scheme": raw.scheme,
            "canonical_value": raw.value,
            "role": "identity",
        }
    if not isinstance(raw, Mapping):
        raise ExternalIdentityInputError(
            "invalid_external_identifier",
            "Each external identifier must be an object.",
            details={"value_type": type(raw).__name__},
        )

    scheme = str(raw.get("scheme") or raw.get("type") or "").strip().casefold()
    if not scheme:
        raise ExternalIdentityInputError(
            "invalid_external_identifier",
            "External identifier scheme is required.",
        )
    if not _SCHEME_PATTERN.fullmatch(scheme):
        raise ExternalIdentityInputError(
            "invalid_external_identifier_scheme",
            "External identifier scheme must use URI-scheme syntax.",
            details={"scheme": scheme},
        )

    role = str(raw.get("role") or "identity").strip().casefold()
    if role != "identity":
        raise ExternalIdentityInputError(
            "invalid_external_identifier_role",
            "create_concepts external identifiers must have role='identity'.",
            details={"role": role},
        )

    raw_value = raw.get("canonical_value")
    if raw_value is None:
        raw_value = raw.get("value")
    if raw_value is None:
        raw_value = raw.get("identifier")
    if not isinstance(raw_value, str):
        raise ExternalIdentityInputError(
            "invalid_external_identifier",
            "External identifier canonical value must be a string.",
            details={"scheme": scheme},
        )
    value = raw_value.strip()
    if not value:
        raise ExternalIdentityInputError(
            "invalid_external_identifier",
            "External identifier canonical value is required.",
            details={"scheme": scheme},
        )
    if len(value) > _MAX_EXTERNAL_IDENTIFIER_VALUE_CHARS:
        raise ExternalIdentityInputError(
            "invalid_external_identifier",
            "External identifier canonical value is too long.",
            details={
                "scheme": scheme,
                "max_chars": _MAX_EXTERNAL_IDENTIFIER_VALUE_CHARS,
            },
        )
    if _CONTROL_CHARACTER_PATTERN.search(value):
        raise ExternalIdentityInputError(
            "invalid_external_identifier",
            "External identifier canonical value cannot contain control characters.",
            details={"scheme": scheme},
        )
    return ExternalIdentifier(scheme=scheme, value=value, source=source)


def normalise_create_external_identifiers(
    *,
    external_identifiers: Any,
    concept_name: str | None = None,
    kind: str | None = None,
) -> tuple[ExternalIdentifier, ...]:
    """Validate explicit identity envelopes without inspecting the entity name."""

    del concept_name, kind
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

    identifiers = [_normalise_explicit_identifier(item) for item in explicit_items]
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


def _identity_marker_relation_candidates(
    identifier: ExternalIdentifier,
) -> ExternalIdentityCandidateScan:
    """Return concepts carrying the exact persisted identity marker."""

    marker = external_identity_marker(identifier)
    fingerprint_prefix = f"{marker.casefold()}||"
    matching_texts_with_sentinel = _consume_search_cursor(
        TextValuesRepository.find(
            {
                "fingerprint": {
                    "$type": "string",
                    "$gte": fingerprint_prefix,
                    "$lt": f"{fingerprint_prefix}\uffff",
                }
            },
            projection={"_id": 1, "text": 1},
            limit=_MAX_EXTERNAL_IDENTITY_TEXT_VALUES + 1,
        )
    )
    text_query_saturated = (
        len(matching_texts_with_sentinel)
        > _MAX_EXTERNAL_IDENTITY_TEXT_VALUES
    )
    matching_texts = matching_texts_with_sentinel[
        :_MAX_EXTERNAL_IDENTITY_TEXT_VALUES
    ]
    text_value_ids: list[Any] = []
    for row in matching_texts:
        if not isinstance(row, Mapping) or row.get("text") != marker:
            continue
        raw_id = row.get("_id")
        if raw_id is None:
            continue
        text_value_ids.append(raw_id)
        raw_id_text = str(raw_id)
        if raw_id_text != raw_id:
            text_value_ids.append(raw_id_text)
    if not text_value_ids:
        return ExternalIdentityCandidateScan(
            exhaustive=not text_query_saturated,
            saturated_stages=(
                ("text_values",) if text_query_saturated else ()
            ),
        )

    relations_with_sentinel = _consume_search_cursor(
        TextRelationsRepository.find(
            {
                "object_text_id": {"$in": text_value_ids},
                "predicate": {"$in": ["hasName", "#V#hasName"]},
            },
            projection={"subject_concept_id": 1},
            limit=_MAX_EXTERNAL_IDENTITY_TEXT_RELATIONS + 1,
        )
    )
    relation_query_saturated = (
        len(relations_with_sentinel)
        > _MAX_EXTERNAL_IDENTITY_TEXT_RELATIONS
    )
    relations = relations_with_sentinel[:_MAX_EXTERNAL_IDENTITY_TEXT_RELATIONS]
    saturated_stages = tuple(
        stage
        for stage, saturated in (
            ("text_values", text_query_saturated),
            ("text_relations", relation_query_saturated),
        )
        if saturated
    )
    return ExternalIdentityCandidateScan(
        candidate_concept_ids=tuple(
            sorted(
                {
                    str(row.get("subject_concept_id")).strip()
                    for row in relations
                    if isinstance(row, Mapping)
                    and isinstance(row.get("subject_concept_id"), str)
                    and str(row.get("subject_concept_id")).strip()
                }
            )
        ),
        exhaustive=not saturated_stages,
        saturated_stages=saturated_stages,
    )


def _text_contains_identifier_value(text: Any, value: str) -> bool:
    """Return whether ``value`` occurs as a bounded opaque token."""

    if not isinstance(text, str) or not text or not value:
        return False
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])")
    return pattern.search(text) is not None


def _legacy_text_reference_candidates(
    identifier: ExternalIdentifier,
) -> ExternalIdentityCandidateScan:
    """Surface indexed textual references without declaring them identities."""

    if len(identifier.value) < _MIN_LEGACY_DISCOVERY_VALUE_CHARS:
        return ExternalIdentityCandidateScan()
    matching_texts_with_sentinel = _consume_search_cursor(
        TextValuesRepository.find(
            {"$text": {"$search": identifier.value}},
            projection={"_id": 1, "text": 1},
            limit=_MAX_EXTERNAL_IDENTITY_TEXT_VALUES + 1,
        )
    )
    text_query_saturated = (
        len(matching_texts_with_sentinel)
        > _MAX_EXTERNAL_IDENTITY_TEXT_VALUES
    )
    matching_texts = matching_texts_with_sentinel[
        :_MAX_EXTERNAL_IDENTITY_TEXT_VALUES
    ]
    text_value_ids: list[Any] = []
    for row in matching_texts:
        if not isinstance(row, Mapping) or not _text_contains_identifier_value(
            row.get("text"),
            identifier.value,
        ):
            continue
        raw_id = row.get("_id")
        if raw_id is None:
            continue
        text_value_ids.append(raw_id)
        raw_id_text = str(raw_id)
        if raw_id_text != raw_id:
            text_value_ids.append(raw_id_text)
    if not text_value_ids:
        return ExternalIdentityCandidateScan(
            exhaustive=not text_query_saturated,
            saturated_stages=(
                ("text_values",) if text_query_saturated else ()
            ),
        )

    relations_with_sentinel = _consume_search_cursor(
        TextRelationsRepository.find(
            {
                "object_text_id": {"$in": text_value_ids},
                "predicate": {"$in": list(_IDENTITY_EVIDENCE_PREDICATES)},
            },
            projection={"subject_concept_id": 1},
            limit=_MAX_EXTERNAL_IDENTITY_TEXT_RELATIONS + 1,
        )
    )
    relation_query_saturated = (
        len(relations_with_sentinel)
        > _MAX_EXTERNAL_IDENTITY_TEXT_RELATIONS
    )
    relations = relations_with_sentinel[:_MAX_EXTERNAL_IDENTITY_TEXT_RELATIONS]
    saturated_stages = tuple(
        stage
        for stage, saturated in (
            ("text_values", text_query_saturated),
            ("text_relations", relation_query_saturated),
        )
        if saturated
    )
    return ExternalIdentityCandidateScan(
        candidate_concept_ids=tuple(
            sorted(
                {
                    str(row.get("subject_concept_id")).strip()
                    for row in relations
                    if isinstance(row, Mapping)
                    and isinstance(row.get("subject_concept_id"), str)
                    and str(row.get("subject_concept_id")).strip()
                }
            )
        ),
        exhaustive=not saturated_stages,
        saturated_stages=saturated_stages,
    )


def _existing_visible_concept_ids(candidate_ids: Sequence[str]) -> set[str]:
    requested_ids = sorted(
        {
            candidate.strip()
            for candidate in candidate_ids
            if isinstance(candidate, str) and candidate.strip()
        }
    )
    if not requested_ids:
        return set()
    visible_ids: set[str] = set()
    # Apply the result bound after actor filtering. A shared marker can exist on
    # many tenant-scoped concepts, so limiting the raw query first can hide the
    # one concept visible to the current actor.
    for start in range(0, len(requested_ids), _MAX_EXTERNAL_IDENTITY_CANDIDATES):
        batch = requested_ids[start : start + _MAX_EXTERNAL_IDENTITY_CANDIDATES]
        existing_ids = {
            str(doc.get("concept_id")).strip()
            for doc in ConceptsRepository.find(
                {"concept_id": {"$in": batch}},
                projection={"concept_id": 1},
                limit=len(batch),
            )
            if isinstance(doc, Mapping)
            and isinstance(doc.get("concept_id"), str)
            and str(doc.get("concept_id")).strip()
        }
        visible_ids.update(filter_accessible_concept_ids(existing_ids))
        # Retain one overflow sentinel so callers can distinguish an exhaustive
        # candidate set from a truncated one.
        if len(visible_ids) > _MAX_EXTERNAL_IDENTITY_CANDIDATES:
            break
    return set(sorted(visible_ids)[: _MAX_EXTERNAL_IDENTITY_CANDIDATES + 1])


def _confirmed_candidate_ids(
    identifier: ExternalIdentifier,
    candidate_ids: Sequence[str],
) -> set[str]:
    """Verify caller-selected candidates against visible persisted evidence."""

    if len(identifier.value) < _MIN_LEGACY_DISCOVERY_VALUE_CHARS:
        return set()
    visible_ids = _existing_visible_concept_ids(candidate_ids)
    if not visible_ids:
        return set()
    marker = external_identity_marker(identifier)
    rows_by_concept = get_texts_for_concepts(
        sorted(visible_ids),
        predicates=_IDENTITY_EVIDENCE_PREDICATES,
        limit_per_concept=_MAX_IDENTITY_EVIDENCE_ROWS_PER_CONCEPT,
    )
    confirmed: set[str] = set()
    for concept_id in visible_ids:
        for row in rows_by_concept.get(concept_id, []):
            text = row.get("text") if isinstance(row, Mapping) else None
            if text == marker or _text_contains_identifier_value(
                text,
                identifier.value,
            ):
                confirmed.add(concept_id)
                break
    return confirmed


def resolve_external_identity_candidates(
    identifier: ExternalIdentifier,
    *,
    canonical_concept_id_candidates: Sequence[str] = (),
    asserted_candidate_concept_ids: Sequence[str] = (),
    rejected_candidate_concept_ids: Sequence[str] = (),
) -> ExternalIdentityResolution:
    """Resolve exact markers or surface legacy evidence for caller judgement."""

    marker_scan = _coerce_candidate_scan(
        _identity_marker_relation_candidates(identifier)
    )
    marker_ids = _existing_visible_concept_ids(
        marker_scan.candidate_concept_ids
    )
    marker_candidate_overflow = (
        len(marker_ids) > _MAX_EXTERNAL_IDENTITY_CANDIDATES
    )
    if not marker_scan.exhaustive or marker_candidate_overflow:
        return ExternalIdentityResolution(
            status="ambiguous",
            identifier=identifier,
            candidate_concept_ids=tuple(
                sorted(marker_ids)[:_MAX_EXTERNAL_IDENTITY_CANDIDATES]
            ),
            resolution_source="persisted_identity_marker_lookup_incomplete",
        )
    if len(marker_ids) == 1:
        return ExternalIdentityResolution(
            status="resolved",
            identifier=identifier,
            candidate_concept_ids=tuple(sorted(marker_ids)),
            resolution_source="persisted_identity_marker",
        )
    if len(marker_ids) > 1:
        return ExternalIdentityResolution(
            status="ambiguous",
            identifier=identifier,
            candidate_concept_ids=tuple(
                sorted(marker_ids)[:_MAX_EXTERNAL_IDENTITY_CANDIDATES]
            ),
            resolution_source="persisted_identity_marker",
        )

    occupied_stable_ids = _existing_visible_concept_ids(
        canonical_concept_id_candidates
    )
    asserted_visible_ids = _existing_visible_concept_ids(
        asserted_candidate_concept_ids
    )
    confirmed_stable_ids = occupied_stable_ids.intersection(asserted_visible_ids)
    if len(confirmed_stable_ids) == 1:
        return ExternalIdentityResolution(
            status="resolved",
            identifier=identifier,
            candidate_concept_ids=tuple(sorted(confirmed_stable_ids)),
            resolution_source="caller_confirmed_stable_identity",
        )
    if len(confirmed_stable_ids) > 1:
        return ExternalIdentityResolution(
            status="ambiguous",
            identifier=identifier,
            candidate_concept_ids=tuple(sorted(confirmed_stable_ids)),
            resolution_source="caller_confirmed_stable_identity",
        )

    confirmed_ids = _confirmed_candidate_ids(
        identifier,
        asserted_candidate_concept_ids,
    )
    if len(confirmed_ids) == 1:
        return ExternalIdentityResolution(
            status="resolved",
            identifier=identifier,
            candidate_concept_ids=tuple(sorted(confirmed_ids)),
            resolution_source="caller_confirmed_visible_evidence",
        )
    if len(confirmed_ids) > 1:
        return ExternalIdentityResolution(
            status="ambiguous",
            identifier=identifier,
            candidate_concept_ids=tuple(sorted(confirmed_ids)),
            resolution_source="caller_confirmed_visible_evidence",
        )

    if occupied_stable_ids:
        return ExternalIdentityResolution(
            status="unverified",
            identifier=identifier,
            candidate_concept_ids=tuple(sorted(occupied_stable_ids)),
            resolution_source="stable_identity_collision",
        )

    legacy_scan = _coerce_candidate_scan(
        _legacy_text_reference_candidates(identifier)
    )
    visible_legacy_ids = _existing_visible_concept_ids(
        legacy_scan.candidate_concept_ids
    )
    legacy_candidate_overflow = (
        not legacy_scan.exhaustive
        or len(visible_legacy_ids) > _MAX_EXTERNAL_IDENTITY_CANDIDATES
    )
    legacy_ids = set(
        sorted(visible_legacy_ids)[:_MAX_EXTERNAL_IDENTITY_CANDIDATES]
    )
    reviewed_non_matches = {
        candidate_id.strip()
        for candidate_id in rejected_candidate_concept_ids
        if isinstance(candidate_id, str) and candidate_id.strip()
    }
    if (
        legacy_ids
        and not legacy_candidate_overflow
        and reviewed_non_matches == legacy_ids
    ):
        return ExternalIdentityResolution(
            status="not_found",
            identifier=identifier,
            resolution_source="caller_rejected_legacy_candidates",
        )
    if legacy_ids:
        return ExternalIdentityResolution(
            status="unverified",
            identifier=identifier,
            candidate_concept_ids=tuple(sorted(legacy_ids)),
            resolution_source=(
                "legacy_text_reference_overflow"
                if legacy_candidate_overflow
                else "legacy_text_reference"
            ),
        )
    if legacy_candidate_overflow:
        return ExternalIdentityResolution(
            status="unverified",
            identifier=identifier,
            resolution_source="legacy_text_reference_overflow",
        )
    return ExternalIdentityResolution(
        status="not_found",
        identifier=identifier,
        resolution_source="no_visible_identity_evidence",
    )


def _scope_stable_external_concept_id(
    *,
    identifier: ExternalIdentifier,
    scope_mode: str | None,
    actor_user_id: str | None,
    actor_org_id: str | None,
) -> str:
    identity_digest = hashlib.sha256(
        _external_identity_material(identifier)
    ).hexdigest()[:16]
    # The value is deliberately absent from the visible concept ID. External
    # identifiers may be private registry keys; the digest provides stable
    # identity without exposing or interpreting the opaque value in receipts,
    # logs, or URLs.
    readable = canonicalise_vontology_concept_id(identifier.scheme)
    readable_slug = (
        readable[3:67]
        if isinstance(readable, str) and readable.startswith("#V#")
        else ""
    )
    if not readable_slug:
        readable_slug = "identifier"
    base_id = f"#V#external_identity_{readable_slug}_{identity_digest}"

    normalised_scope_mode = str(scope_mode or "").strip().casefold()
    canonical_user_id = canonicalise_vontology_concept_id(actor_user_id)
    canonical_org_id = canonicalise_vontology_concept_id(actor_org_id)
    if normalised_scope_mode == "global_general" or not (
        canonical_user_id or canonical_org_id
    ):
        return base_id

    visibility_scope = (
        f"organisation:{canonical_org_id}"
        if canonical_org_id
        else f"user:{canonical_user_id}"
    )
    scope_digest = hashlib.sha256(
        visibility_scope.casefold().encode("utf-8")
    ).hexdigest()[:10]
    return f"{base_id}_scope_{scope_digest}"


def canonical_concept_id_for_external_identifiers(
    identifiers: Sequence[ExternalIdentifier],
    *,
    kind: str,
    parent_id: str | None,
    scope_mode: str | None = None,
    actor_user_id: str | None = None,
    actor_org_id: str | None = None,
) -> str | None:
    """Return a stable actor-scoped ID without interpreting entity semantics."""

    del parent_id
    if len(identifiers) != 1:
        return None
    normalised_kind = str(kind or "").strip().casefold()
    if normalised_kind == "individual":
        normalised_kind = "instance"
    if normalised_kind not in {"instance", "type", "predicate"}:
        return None
    return _scope_stable_external_concept_id(
        identifier=identifiers[0],
        scope_mode=scope_mode,
        actor_user_id=actor_user_id,
        actor_org_id=actor_org_id,
    )


def persist_external_identity_markers(
    *,
    concept_id: str,
    identifiers: Sequence[ExternalIdentifier],
) -> dict[str, Any]:
    """Persist exact markers and read back any exceptional write outcome."""

    writes: list[dict[str, Any]] = []
    indeterminate_failures: list[dict[str, Any]] = []
    changed_values: list[bool | None] = []
    for identifier in identifiers:
        marker = external_identity_marker(identifier)
        try:
            result = upsert_text_for_concept(
                subject_concept_id=concept_id,
                predicate="hasName",
                text=marker,
                lang="und",
                provenance={
                    "source": "create_concepts_external_identity",
                    "external_identifier_scheme": identifier.scheme,
                },
                context={
                    "name_type": "CODE",
                    "source": "create_concepts_external_identity",
                    "identity_marker": True,
                },
            )
            writes.append(
                {
                    "scheme": identifier.scheme,
                    "value": identifier.value,
                    "marker": marker,
                    "relation_id": result.get("relation_id"),
                    "relation_created": bool(result.get("relation_created")),
                    "context_updated": bool(result.get("context_updated")),
                }
            )
            changed_values.append(
                bool(
                    result.get("relation_created")
                    or result.get("context_updated")
                )
            )
        except Exception as exc:
            readback_error: str | None = None
            try:
                marker_scan = _coerce_candidate_scan(
                    _identity_marker_relation_candidates(identifier)
                )
                marker_concept_ids = set(marker_scan.candidate_concept_ids)
                if not marker_scan.exhaustive and concept_id not in marker_concept_ids:
                    readback_error = (
                        "Exact marker readback was not exhaustive"
                        + (
                            ": " + ", ".join(marker_scan.saturated_stages)
                            if marker_scan.saturated_stages
                            else ""
                        )
                    )
            except Exception as readback_exc:
                marker_concept_ids = set()
                readback_error = str(readback_exc)
            if concept_id in marker_concept_ids:
                writes.append(
                    {
                        "scheme": identifier.scheme,
                        "value": identifier.value,
                        "marker": marker,
                        "relation_id": None,
                        "relation_created": None,
                        "context_updated": None,
                        "write_outcome": "readback_verified_after_exception",
                        "write_error": str(exc),
                        "write_exception_type": type(exc).__name__,
                    }
                )
                changed_values.append(None)
                continue
            indeterminate_failures.append(
                {
                    "stage": "external_identity_persistence",
                    "scheme": identifier.scheme,
                    "value": identifier.value,
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                    "outcome": "indeterminate",
                    "readback_verified": False,
                    "readback_error": readback_error,
                }
            )
            changed_values.append(None)

    if indeterminate_failures:
        effect_status = "indeterminate"
        changed: bool | None = (
            True if any(value is True for value in changed_values) else None
        )
    else:
        effect_status = "succeeded"
        changed = (
            None
            if any(value is None for value in changed_values)
            else any(value is True for value in changed_values)
        )
    return {
        "success": not indeterminate_failures,
        "effect_status": effect_status,
        "changed": changed,
        "writes": writes,
        "failures": [],
        "indeterminate_failures": indeterminate_failures,
    }


__all__ = [
    "ExternalIdentifier",
    "ExternalIdentityInputError",
    "ExternalIdentityResolution",
    "canonical_concept_id_for_external_identifiers",
    "external_identity_marker",
    "normalise_create_external_identifiers",
    "persist_external_identity_markers",
    "resolve_external_identity_candidates",
]
