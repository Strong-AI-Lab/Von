"""Service functions for TextValue documents and their relations.

Provides create, link, upsert, and fetch helpers around the text_values and
text_relations repositories, with simple normalization and validation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    cast,
)

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError

from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..models.text_value_models import (
    RelationPredicate,
    TextRelationModel,
    TextValueModel,
)
from ..security.access_control import (
    can_access_concept,
    filter_accessible_concept_ids,
)
from .feature_flags import (
    get_event_workflow_integration_enabled,
    get_workflow_discovery_cache_invalidation_enabled,
)
from .text_relation_read_policy import (
    generic_text_read_predicate_filter,
    is_hidden_from_generic_text_reads,
)

_EVENT_TYPE_TEXT_RELATION_UPSERTED = "text_relation.upserted"
_EVENT_TYPE_TEXT_RELATION_UPDATED = "text_relation.updated"
_EVENT_TYPE_TEXT_RELATION_DELETED = "text_relation.deleted"

TextContextView = Literal["base_publication", "actor_effective"]
_TEXT_CONTEXT_VIEWS = frozenset({"base_publication", "actor_effective"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: Any) -> str | None:
    resolved = _as_utc_datetime(value)
    if resolved is not None:
        return resolved.isoformat()
    return None


def _as_utc_datetime(value: Any) -> datetime | None:
    resolved: datetime
    if isinstance(value, datetime):
        resolved = value
    elif isinstance(value, str) and value.strip():
        try:
            resolved = datetime.fromisoformat(
                value.strip().replace("Z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _row_timestamp(row: Dict[str, Any]) -> datetime:
    resolved = _as_utc_datetime(
        row.get("relation_updated_at") or row.get("relation_created_at")
    )
    return resolved or datetime.min.replace(tzinfo=timezone.utc)


def _invalidate_stats_for_predicate_change(predicate: str | None) -> None:
    """Best-effort stats invalidation for ontology predicate extent changes."""

    if not isinstance(predicate, str) or not predicate.startswith("#V#"):
        return
    try:
        from .vontology_concept_stats_service import (
            invalidate_vontology_concept_stats_cache,
        )

        invalidate_vontology_concept_stats_cache(
            reason="predicate_extent_mutation",
            affected_concepts=[predicate],
        )
    except Exception:
        pass


def _invalidate_workflow_routing_projection_for_text_relation_change(
    *,
    subject_concept_id: str,
    predicate: str | None,
) -> None:
    """Best-effort invalidation for workflow-routing support projections."""

    if not get_workflow_discovery_cache_invalidation_enabled(default=True):
        return

    predicate_text = str(predicate or "").strip()
    if not predicate_text:
        return
    try:
        from ..workflows.vontology_loader import (
            WORKFLOW_DISCOVERY_EXEMPLARS_TEXT_PREDICATE_PRECEDENCE,
            WORKFLOW_PUBLICATION_LIFECYCLE_TEXT_PREDICATE_PRECEDENCE,
            WORKFLOW_ROUTING_DESCRIPTION_TEXT_PREDICATE_PRECEDENCE,
            WORKFLOW_ROUTING_PROFILE_TEXT_PREDICATE_PRECEDENCE,
        )

        relevant_predicates = {
            item
            for precedence in (
                WORKFLOW_DISCOVERY_EXEMPLARS_TEXT_PREDICATE_PRECEDENCE,
                WORKFLOW_PUBLICATION_LIFECYCLE_TEXT_PREDICATE_PRECEDENCE,
                WORKFLOW_ROUTING_DESCRIPTION_TEXT_PREDICATE_PRECEDENCE,
                WORKFLOW_ROUTING_PROFILE_TEXT_PREDICATE_PRECEDENCE,
            )
            for group in precedence
            for item in group
        }
        if predicate_text not in relevant_predicates:
            return
    except Exception:
        return

    try:
        from .workflow_capability_service import (
            invalidate_workflow_capability_index,
            is_authoritative_workflow_concept_id,
        )
        from .workflow_discovery_service import (
            invalidate_workflow_discovery_executability_caches,
        )

        # Scope invalidation to concepts the capability index actually tracks.
        # Background workflows continually rewrite descriptions on unrelated
        # (non-workflow) concepts; without this guard every such write churns
        # the routing index and starves live workflow discovery of a ready
        # index. New workflows are indexed via the registration path, so this
        # only suppresses no-op invalidations from non-workflow subjects.
        if not is_authoritative_workflow_concept_id(subject_concept_id):
            return

        invalidate_workflow_capability_index(
            reason=(
                "workflow_routing_text_relation_changed:"
                f"{subject_concept_id}:{predicate_text}"
            ),
            # The persisted capability manifest embeds this represented routing
            # metadata.  Dropping only the process cache can immediately reload
            # the stale manifest and silently undo the semantic update.
            reset_backend_namespace=True,
        )
        invalidate_workflow_discovery_executability_caches()
    except Exception:
        pass


def _emit_text_relation_mutation_event(
    *,
    event_type: str,
    subject_concept_id: str,
    relation_id: str | None = None,
    predicate: str | None = None,
    text: str | None = None,
    lang: str | None = None,
    extra_payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort workflow event emission for text-relation mutations."""

    # Authentication identifiers have a dedicated, actor-scoped governance
    # lifecycle.  Generic workflow events persist their inputs for broader
    # inspection, so neither the predicate nor its plaintext value belongs on
    # that channel.
    if is_hidden_from_generic_text_reads(predicate):
        return

    if not get_event_workflow_integration_enabled(default=True):
        return

    try:
        from .workflow_event_integration_service import (
            maybe_launch_vontology_mutation_workflow,
            resolve_event_actor_context,
        )

        actor_id, actor_org = resolve_event_actor_context()
        payload: Dict[str, Any] = {
            "subject_concept_id": subject_concept_id,
            "relation_id": relation_id,
            "predicate": predicate,
            "text": text,
            "lang": lang,
        }
        if isinstance(extra_payload, dict):
            payload.update(extra_payload)
        mutation_id = (
            relation_id
            if isinstance(relation_id, str) and relation_id.strip()
            else f"{subject_concept_id}:{predicate or 'unknown'}"
        )
        maybe_launch_vontology_mutation_workflow(
            mutation_event_type=event_type,
            mutation_id=mutation_id,
            user_id=actor_id,
            org_id=actor_org,
            event_payload=payload,
            inputs=dict(payload),
        )
    except Exception:
        pass


def _normalize_text(raw: str) -> str:
    """Normalise text for fingerprint/dedup purposes.

    This is deliberately *less* aggressive than a full whitespace collapse.
    Newlines are meaningful for Markdown and other formatted text, so we preserve
    them in the fingerprint and only normalise intra-line whitespace.
    """
    text = raw.strip()
    # Normalise line endings so equivalent text across platforms dedups.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of non-newline whitespace within lines.
    text = re.sub(r"[\t\f\v ]+", " ", text)
    # Strip incidental spaces around newlines (does not remove the newlines).
    text = re.sub(r" *\n *", "\n", text)
    return text


def _prepare_persisted_text(raw: str) -> str:
    """Light cleanup for storage: trim ends but preserve internal newlines/spacing."""
    return raw.strip()


def _compute_fingerprint(text: str, lang: str) -> str:
    # Normalize: collapse whitespace, lowercase, include lang to avoid cross-lang collisions
    normalized = _normalize_text(text).lower()
    return f"{normalized}||{lang.lower()}"


def create_text_value(
    text: str, lang: str = "en", provenance: Optional[Dict[str, Any]] = None
) -> str:
    """Create or return an existing TextValue document based on (fingerprint, lang).

    Returns the inserted/existing ObjectId as a hex string.
    """
    provenance = provenance or {}
    stored_text = _prepare_persisted_text(text)
    fp = _compute_fingerprint(stored_text, lang)

    # Validate input via Pydantic (stores the preserved formatting)
    tv = TextValueModel(text=stored_text, lang=lang, provenance=provenance)

    # Try to find by fingerprint+lang
    existing = TextValuesRepository.find_one({"fingerprint": fp, "lang": tv.lang})
    if existing and existing.get("_id"):
        return str(existing["_id"])

    doc = {
        "text": tv.text,
        "lang": tv.lang,
        "provenance": provenance,
        "fingerprint": fp,
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        res = TextValuesRepository.insert_one(doc)
    except DuplicateKeyError:
        # A concurrent creator can win after the lookup above.  The unique
        # fingerprint/lang index remains authoritative, so reconcile to that
        # exact canonical row rather than failing an otherwise idempotent
        # upsert.
        existing = TextValuesRepository.find_one({"fingerprint": fp, "lang": tv.lang})
        if existing and existing.get("_id"):
            return str(existing["_id"])
        raise
    return str(res.inserted_id)


def link_text_to_concept(
    subject_concept_id: str,
    predicate: str,
    object_text_id: str,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[str, bool, bool]:
    """Create a relation linking a concept to a text value.

    Enforces uniqueness via collection unique index on (subject, predicate, object).
    Returns (relation id, created_flag, context_updated_flag).
    """
    if not can_access_concept(subject_concept_id):
        raise PermissionError("User cannot link text to an inaccessible concept")
    context = context or {}
    # Validate relation via Pydantic (predicate constrained)
    _ = TextRelationModel(
        subject_concept_id=subject_concept_id,
        predicate=cast(
            Literal[
                "hasName", "hasNote", "hasDescription", "hasInteraction", "hasContent"
            ],
            predicate,
        ),  # Literal validated in model
        object_text_id=object_text_id,
        context=context,
    )
    relation = {
        "subject_concept_id": subject_concept_id,
        "predicate": predicate,
        "object_text_id": object_text_id,
        "context": context,
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        res = TextRelationsRepository.insert_one(relation)
        return str(res.inserted_id), True, False
    except DuplicateKeyError:
        # Already linked; fetch existing id
        existing = TextRelationsRepository.find_one(
            {
                "subject_concept_id": subject_concept_id,
                "predicate": predicate,
                "object_text_id": object_text_id,
            }
        )
        context_updated = False
        if existing and context:
            existing_context = existing.get("context") or {}
            if existing_context != context:
                TextRelationsRepository.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {"context": context, "updated_at": _now()}},
                )
                context_updated = True
        return (
            str(existing["_id"]) if existing and existing.get("_id") else "",
            False,
            context_updated,
        )


def upsert_text_for_concept(
    subject_concept_id: str,
    predicate: str,
    text: str,
    lang: str = "en",
    provenance: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convenience: ensure a TextValue exists and is linked to the concept.

    Returns a payload with text_value_id and relation_id.
    """
    stored_text = _prepare_persisted_text(text)
    text_value_id = create_text_value(
        text=stored_text, lang=lang, provenance=provenance
    )
    relation_id, relation_created, context_updated = link_text_to_concept(
        subject_concept_id=subject_concept_id,
        predicate=predicate,
        object_text_id=text_value_id,
        context=context,
    )

    # JVNAUTOSCI-680: Mark concept embedding as stale when text relations change
    if relation_created or context_updated:
        try:
            from ..db.repositories.concepts_repository import ConceptsRepository

            ConceptsRepository.update_one(
                {"concept_id": subject_concept_id},
                {"$set": {"embedding_status": "stale"}},
            )
        except Exception:
            pass  # Best effort - don't fail the main operation

    if relation_created:
        _invalidate_stats_for_predicate_change(predicate)
    if relation_created:
        # Workflow routing projections are derived from the relation text and
        # predicate, not relation provenance/context. Context-only seed
        # refreshes must not churn the capability index.
        _invalidate_workflow_routing_projection_for_text_relation_change(
            subject_concept_id=subject_concept_id,
            predicate=predicate,
        )

    relation_doc = None
    if relation_id:
        try:
            relation_doc = TextRelationsRepository.find_one(
                {"_id": ObjectId(relation_id)}
            )
        except (InvalidId, TypeError):
            relation_doc = None

    payload: Dict[str, Any] = {
        "text_value_id": text_value_id,
        "relation_id": relation_id,
        "relation_created": relation_created,
        "context_updated": context_updated,
        "text": stored_text,
        "lang": lang,
    }
    if relation_doc:
        payload["context"] = relation_doc.get("context", {})
        payload["predicate"] = relation_doc.get("predicate")
    elif context:
        payload["context"] = context

    if relation_created or context_updated:
        _emit_text_relation_mutation_event(
            event_type=_EVENT_TYPE_TEXT_RELATION_UPSERTED,
            subject_concept_id=subject_concept_id,
            relation_id=relation_id,
            predicate=predicate,
            text=stored_text,
            lang=lang,
            extra_payload={
                "relation_created": relation_created,
                "context_updated": context_updated,
                "text_value_id": text_value_id,
            },
        )

    return payload


def get_texts_for_concept(
    subject_concept_id: str,
    predicate: Optional[str] = None,
    lang: Optional[str] = None,
    limit: int = 50,
    recent_first: bool = False,
    max_time_ms: Optional[int] = None,
    *,
    context_view: TextContextView = "base_publication",
) -> List[Dict[str, Any]]:
    """Fetch text assertions in an explicit semantic view.

    ``base_publication`` preserves the repository's historical relation-store
    read. ``actor_effective`` overlays scoped assertions visible to the current
    trusted actor. Storage location alone must not choose the view.
    """
    rows_by_concept = get_texts_for_concepts(
        [subject_concept_id],
        predicate=predicate,
        lang=lang,
        limit_per_concept=limit,
        recent_first=recent_first,
        max_time_ms=max_time_ms,
        context_view=context_view,
    )
    return rows_by_concept.get(subject_concept_id, [])


def get_texts_for_concepts(
    subject_concept_ids: Sequence[str],
    *,
    predicate: Optional[str] = None,
    predicates: Optional[Sequence[str]] = None,
    lang: Optional[str] = None,
    limit_per_concept: int = 50,
    recent_first: bool = False,
    max_time_ms: Optional[int] = None,
    query_metadata: Optional[Dict[str, Any]] = None,
    context_view: TextContextView = "base_publication",
) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch text assertions for many concepts in an explicit semantic view.

    Returns rows grouped by concept id. This is deliberately generic support for
    Vontology-heavy paths such as workflow publication/read-back, where many
    step concepts need their policy text relations at once.
    """
    if context_view not in _TEXT_CONTEXT_VIEWS:
        raise ValueError(
            "context_view must be 'base_publication' or 'actor_effective'"
        )

    candidate_subject_ids: List[str] = []
    seen_subject_ids: set[str] = set()
    for raw_id in subject_concept_ids:
        if not isinstance(raw_id, str):
            continue
        subject_concept_id = raw_id.strip()
        if not subject_concept_id or subject_concept_id in seen_subject_ids:
            continue
        seen_subject_ids.add(subject_concept_id)
        candidate_subject_ids.append(subject_concept_id)

    accessible_subject_ids = filter_accessible_concept_ids(candidate_subject_ids)
    ordered_subject_ids = [
        subject_id
        for subject_id in candidate_subject_ids
        if subject_id in accessible_subject_ids
    ]

    if not ordered_subject_ids:
        if query_metadata is not None:
            query_metadata.update(
                {
                    "raw_relation_count": 0,
                    "relation_query_limit": 0,
                    "relation_query_truncated": False,
                    "context_view": context_view,
                    "scoped_assertion_count": 0,
                    "scoped_query_limit": 0,
                    "scoped_query_truncated": False,
                }
            )
        return {}

    rel_filter: Dict[str, Any] = {
        "subject_concept_id": {"$in": ordered_subject_ids},
        "predicate": generic_text_read_predicate_filter(
            predicate=predicate,
            predicates=predicates,
        ),
    }

    sort = [("updated_at", -1), ("created_at", -1)] if recent_first else None
    per_concept_relation_limit = max(limit_per_concept, 1)
    if query_metadata is not None:
        # One extra raw relation is a completeness sentinel. Joined text rows
        # cannot provide this signal because dangling TextValue references are
        # deliberately omitted from the projection below.
        per_concept_relation_limit += 1
    relation_limit = max(
        len(ordered_subject_ids) * per_concept_relation_limit,
        1,
    )
    relations = list(
        TextRelationsRepository.find(
            rel_filter,
            sort=sort,
            limit=relation_limit,
            max_time_ms=max_time_ms,
        )
    )
    if query_metadata is not None:
        query_metadata.update(
            {
                "raw_relation_count": len(relations),
                "relation_query_limit": relation_limit,
                "relation_query_truncated": len(relations) >= relation_limit,
            }
        )
    rows_by_concept: Dict[str, List[Dict[str, Any]]] = {
        subject_id: [] for subject_id in ordered_subject_ids
    }
    base_rows = (
        _resolve_text_rows_from_relations(
            relations,
            lang=lang,
            limit=None,
            max_time_ms=max_time_ms,
        )
        if relations
        else []
    )
    scoped_rows: List[Dict[str, Any]] = []
    scoped_query_limit = 0
    scoped_query_truncated = False
    scoped_visibility_filtered = False
    scoped_counts_are_lower_bounds = False
    scoped_per_concept: Dict[str, Dict[str, Any]] = {}
    if context_view == "actor_effective":
        from .scoped_assertion_service import (
            MAX_PAGE_CANDIDATES,
            MAX_PAGE_LIMIT,
            MAX_PAGE_SUBJECTS,
            list_visible_scoped_assertions_page,
            scoped_text_assertion_to_relation_row,
        )

        requested_per_concept = max(limit_per_concept, 1)
        scoped_per_concept_limit = min(
            requested_per_concept,
            MAX_PAGE_LIMIT,
        )
        max_subjects_per_query = min(
            MAX_PAGE_SUBJECTS,
            max(
                1,
                MAX_PAGE_CANDIDATES // (scoped_per_concept_limit + 1),
            ),
        )
        requested_scoped_predicates = (
            [predicate]
            if isinstance(predicate, str) and predicate.strip()
            else [
                str(item).strip()
                for item in predicates or ()
                if isinstance(item, str) and str(item).strip()
            ]
        )
        scoped_predicates = [
            item
            for item in requested_scoped_predicates
            if not is_hidden_from_generic_text_reads(item)
        ]
        scoped_predicate_request_filtered_out = bool(
            requested_scoped_predicates and not scoped_predicates
        )
        if requested_per_concept > scoped_per_concept_limit:
            scoped_query_truncated = True
            scoped_counts_are_lower_bounds = True
        for batch_start in (
            range(0, len(ordered_subject_ids), max_subjects_per_query)
            if not scoped_predicate_request_filtered_out
            else ()
        ):
            subject_batch = ordered_subject_ids[
                batch_start : batch_start + max_subjects_per_query
            ]
            scoped_page = list_visible_scoped_assertions_page(
                subject_concept_ids=subject_batch,
                predicates=scoped_predicates or None,
                object_kind="text",
                languages=[lang] if lang else None,
                limit=min(
                    MAX_PAGE_LIMIT,
                    max(len(subject_batch) * scoped_per_concept_limit, 1),
                ),
                limit_per_subject=scoped_per_concept_limit,
            )
            scoped_query_limit += (
                len(subject_batch) * scoped_per_concept_limit
            )
            scoped_query_truncated = bool(
                scoped_query_truncated or scoped_page.get("truncated")
            )
            scoped_visibility_filtered = bool(
                scoped_visibility_filtered
                or scoped_page.get("visibility_filtered")
            )
            scoped_counts_are_lower_bounds = bool(
                scoped_counts_are_lower_bounds
                or scoped_page.get("counts_are_lower_bounds")
            )
            page_by_subject = scoped_page.get("per_subject")
            if isinstance(page_by_subject, Mapping):
                for subject_id, subject_page in page_by_subject.items():
                    if not isinstance(subject_page, Mapping):
                        continue
                    scoped_per_concept[str(subject_id)] = {
                        "returned": int(subject_page.get("returned") or 0),
                        "limit": subject_page.get("limit"),
                        "has_more": bool(subject_page.get("has_more")),
                        "visibility_filtered": bool(
                            subject_page.get("visibility_filtered")
                        ),
                        "counts_are_lower_bounds": bool(
                            subject_page.get("counts_are_lower_bounds")
                        ),
                    }
            for assertion in scoped_page.get("items") or ():
                if is_hidden_from_generic_text_reads(assertion.get("predicate")):
                    continue
                scoped_row = scoped_text_assertion_to_relation_row(assertion)
                if scoped_row is None:
                    continue
                scoped_rows.append(scoped_row)

    if query_metadata is not None:
        query_metadata.update(
            {
                "context_view": context_view,
                "scoped_assertion_count": len(scoped_rows),
                "scoped_query_limit": scoped_query_limit,
                "scoped_query_truncated": scoped_query_truncated,
                "scoped_visibility_filtered": scoped_visibility_filtered,
                "scoped_counts_are_lower_bounds": (
                    scoped_counts_are_lower_bounds
                ),
                "scoped_per_concept": scoped_per_concept,
            }
        )

    if recent_first:
        rows = [*base_rows, *scoped_rows]
        rows.sort(key=_row_timestamp, reverse=True)
    else:
        # Direct actor-scoped deltas are surfaced before the base rows in a
        # bounded effective view so a small limit cannot make the actor's own
        # assertion disappear. This is retrieval priority, not logical
        # override: both row kinds remain explicitly labelled.
        rows = (
            [*scoped_rows, *base_rows]
            if context_view == "actor_effective"
            else base_rows
        )
    for row in rows:
        subject_concept_id = row.get("subject_concept_id")
        if not isinstance(subject_concept_id, str):
            continue
        concept_rows = rows_by_concept.get(subject_concept_id)
        if concept_rows is None or len(concept_rows) >= limit_per_concept:
            continue
        concept_rows.append(row)

    return rows_by_concept


def _resolve_text_rows_from_relations(
    relations: Sequence[Dict[str, Any]],
    *,
    lang: Optional[str] = None,
    limit: Optional[int] = None,
    max_time_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Resolve relation documents into joined text rows."""

    object_id_values: List[ObjectId] = []
    string_id_values: List[str] = []
    for relation in relations:
        if is_hidden_from_generic_text_reads(relation.get("predicate")):
            continue
        raw_id = relation.get("object_text_id")
        if not raw_id:
            continue
        if isinstance(raw_id, ObjectId):
            object_id_values.append(raw_id)
            continue
        if isinstance(raw_id, str):
            try:
                object_id_values.append(ObjectId(raw_id))
                continue
            except (InvalidId, TypeError):
                string_id_values.append(raw_id)
                continue
        string_id_values.append(str(raw_id))

    values: Dict[str, Any] = {}
    if object_id_values:
        tv_filter: Dict[str, Any] = {"_id": {"$in": object_id_values}}
        if lang:
            tv_filter["lang"] = lang
        for tv in TextValuesRepository.find(tv_filter, max_time_ms=max_time_ms):
            values[str(tv["_id"])] = tv

    if string_id_values:
        tv_filter: Dict[str, Any] = {"_id": {"$in": string_id_values}}
        if lang:
            tv_filter["lang"] = lang
        for tv in TextValuesRepository.find(tv_filter, max_time_ms=max_time_ms):
            values[str(tv["_id"])] = tv

    results: List[Dict[str, Any]] = []
    for r in relations:
        if is_hidden_from_generic_text_reads(r.get("predicate")):
            continue
        key = r.get("object_text_id")
        if isinstance(key, ObjectId):
            lookup_key = str(key)
        else:
            lookup_key = str(key) if key is not None else None
        tv = values.get(lookup_key) if lookup_key is not None else None
        if not tv:
            continue
        results.append(
            {
                "subject_concept_id": r.get("subject_concept_id"),
                "text": tv.get("text"),
                "lang": tv.get("lang"),
                "text_value_id": str(tv.get("_id")),
                "predicate": r.get("predicate"),
                "relation_id": str(r.get("_id")) if r.get("_id") else None,
                "row_kind": "base_text_relation",
                "context": r.get("context", {}),
                "provenance": tv.get("provenance", {}),
                "text_value_created_at": _iso_utc(tv.get("created_at")),
                "text_value_updated_at": _iso_utc(tv.get("updated_at")),
                "relation_created_at": _iso_utc(r.get("created_at")),
                "relation_updated_at": _iso_utc(r.get("updated_at")),
            }
        )
        if limit is not None and len(results) >= limit:
            break
    return results


def _normalise_predicate_precedence(
    predicate_precedence: Optional[Sequence[str | Sequence[str]]],
) -> List[Tuple[str, ...]]:
    normalised_precedence: List[Tuple[str, ...]] = []
    for item in predicate_precedence or []:
        if isinstance(item, str):
            token = item.strip()
            if token:
                normalised_precedence.append((token,))
            continue
        if isinstance(item, Sequence):
            aliases = tuple(
                str(alias).strip()
                for alias in item
                if isinstance(alias, str) and alias.strip()
            )
            if aliases:
                normalised_precedence.append(aliases)
    return normalised_precedence


def _build_language_rank(
    preferred_languages: Sequence[str],
) -> Tuple[Dict[str, int], int]:
    normalised_languages = [
        lang.strip().lower()
        for lang in preferred_languages
        if isinstance(lang, str) and lang.strip()
    ]
    language_rank = {lang: idx for idx, lang in enumerate(normalised_languages)}
    default_language_rank = len(language_rank) + 1
    return language_rank, default_language_rank


def _row_sort_key(
    row: Dict[str, Any],
    *,
    language_rank: Dict[str, int],
    default_language_rank: int,
) -> tuple[int, str, str]:
    row_lang = row.get("lang")
    lang_token = row_lang.strip().lower() if isinstance(row_lang, str) else ""
    lang_priority = language_rank.get(lang_token, default_language_rank)
    predicate = str(row.get("predicate") or "")
    relation_id = str(row.get("relation_id") or "")
    return (lang_priority, predicate, relation_id)


def _select_preferred_text_row(
    rows: Sequence[Dict[str, Any]],
    *,
    predicate_precedence: Optional[Sequence[str | Sequence[str]]] = None,
    preferred_languages: Sequence[str] = ("en-NZ", "en"),
) -> Optional[Dict[str, Any]]:
    filtered_rows: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        filtered_rows.append(row)

    if not filtered_rows:
        return None

    normalised_precedence = _normalise_predicate_precedence(predicate_precedence)
    language_rank, default_language_rank = _build_language_rank(preferred_languages)

    def _sort_key(row: Dict[str, Any]) -> tuple[int, str, str]:
        return _row_sort_key(
            row,
            language_rank=language_rank,
            default_language_rank=default_language_rank,
        )

    if normalised_precedence:
        for predicate_group in normalised_precedence:
            candidates = [
                row
                for row in filtered_rows
                if str(row.get("predicate") or "").strip() in predicate_group
            ]
            if not candidates:
                continue
            candidates.sort(key=_sort_key)
            return dict(candidates[0])

    filtered_rows.sort(key=_sort_key)
    return dict(filtered_rows[0])


def get_preferred_texts_for_concepts(
    subject_concept_ids: Sequence[str],
    *,
    predicate_precedence: Optional[Sequence[str | Sequence[str]]] = None,
    preferred_languages: Sequence[str] = ("en-NZ", "en"),
    limit_per_concept: int = 50,
) -> Dict[str, Dict[str, Any]]:
    """Return the highest-priority text relation for each accessible concept."""

    ordered_subject_ids: List[str] = []
    seen_subject_ids: set[str] = set()
    for raw_id in subject_concept_ids:
        if not isinstance(raw_id, str):
            continue
        subject_concept_id = raw_id.strip()
        if (
            not subject_concept_id
            or subject_concept_id in seen_subject_ids
            or not can_access_concept(subject_concept_id)
        ):
            continue
        seen_subject_ids.add(subject_concept_id)
        ordered_subject_ids.append(subject_concept_id)

    if not ordered_subject_ids:
        return {}

    normalised_precedence = _normalise_predicate_precedence(predicate_precedence)
    allowed_predicates = (
        sorted(
            {
                predicate
                for predicate_group in normalised_precedence
                for predicate in predicate_group
            }
        )
        if normalised_precedence
        else None
    )
    rows_by_subject = get_texts_for_concepts(
        ordered_subject_ids,
        predicates=allowed_predicates,
        limit_per_concept=limit_per_concept,
    )

    preferred_rows: Dict[str, Dict[str, Any]] = {}
    for subject_concept_id, subject_rows in rows_by_subject.items():
        preferred_row = _select_preferred_text_row(
            subject_rows,
            predicate_precedence=normalised_precedence,
            preferred_languages=preferred_languages,
        )
        if preferred_row is not None:
            preferred_rows[subject_concept_id] = preferred_row

    return preferred_rows


def get_preferred_text_for_concept(
    subject_concept_id: str,
    *,
    predicate_precedence: Optional[Sequence[str | Sequence[str]]] = None,
    preferred_languages: Sequence[str] = ("en-NZ", "en"),
    limit: int = 200,
) -> Optional[Dict[str, Any]]:
    """Return the highest-priority text relation for a concept.

    Args:
        subject_concept_id: Concept identifier to inspect.
        predicate_precedence: Ordered predicate groups. Each group can be a
            single predicate string or an iterable of aliases that are treated
            as equivalent at that precedence level.
        preferred_languages: Ordered language preference list.
        limit: Max relation rows to inspect.
    """
    preferred_rows = get_preferred_texts_for_concepts(
        [subject_concept_id],
        predicate_precedence=predicate_precedence,
        preferred_languages=preferred_languages,
        limit_per_concept=limit,
    )
    return preferred_rows.get(subject_concept_id)


def update_text_relation_text(
    subject_concept_id: str,
    relation_id: str,
    new_text: str,
    lang: str = "en",
    provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Update the text tied to a specific relation.

    Strategy:
      * Locate relation (validate subject)
      * If existing TextValue already has new_text (case/whitespace normalized) do nothing
      * Else find or create a TextValue for new_text (dedup via fingerprint)
      * Update relation.object_text_id accordingly & timestamps
    Returns minimal payload with updated relation + text contents.
    """
    if not can_access_concept(subject_concept_id):
        raise PermissionError("User cannot update text for an inaccessible concept")
    provenance = provenance or {}

    try:
        rel = TextRelationsRepository.find_one({"_id": ObjectId(relation_id)})
    except (InvalidId, TypeError):
        rel = None
    if not rel:
        raise ValueError("Relation not found")
    if rel.get("subject_concept_id") != subject_concept_id:
        raise ValueError("Relation subject mismatch")

    lang = (lang or "en").strip() or "en"

    # Fetch current text value if available so we can decide whether an update is needed
    current_tv_id = rel.get("object_text_id")
    current_tv_id_str = str(current_tv_id) if current_tv_id is not None else None
    current_tv = None
    if current_tv_id:
        try:
            current_tv = TextValuesRepository.find_one({"_id": ObjectId(current_tv_id)})
        except (InvalidId, TypeError):
            current_tv = None

    new_raw = _prepare_persisted_text(new_text)
    if (
        current_tv
        and current_tv.get("text") == new_raw
        and (current_tv.get("lang") or lang) == lang
    ):
        return {
            "relation_id": relation_id,
            "text_value_id": current_tv_id_str,
            "text": new_raw,
            "lang": lang,
            "predicate": rel.get("predicate"),
            "updated": False,
        }

    # Reuse create_text_value for fingerprint logic to avoid duplicate documents for whitespace-only changes
    new_text_value_id = create_text_value(
        text=new_raw, lang=lang, provenance=provenance
    )
    relation_updated = False
    if new_text_value_id != current_tv_id_str:
        TextRelationsRepository.update_one(
            {"_id": ObjectId(relation_id)},
            {"$set": {"object_text_id": new_text_value_id, "updated_at": _now()}},
        )
        relation_updated = True

    result = {
        "relation_id": relation_id,
        "text_value_id": new_text_value_id,
        "text": new_raw,
        "lang": lang,
        "predicate": rel.get("predicate"),
        "updated": relation_updated,
    }
    if relation_updated:
        _invalidate_workflow_routing_projection_for_text_relation_change(
            subject_concept_id=subject_concept_id,
            predicate=str(rel.get("predicate") or ""),
        )
        _emit_text_relation_mutation_event(
            event_type=_EVENT_TYPE_TEXT_RELATION_UPDATED,
            subject_concept_id=subject_concept_id,
            relation_id=relation_id,
            predicate=str(rel.get("predicate") or ""),
            text=new_raw,
            lang=lang,
            extra_payload={"text_value_id": new_text_value_id},
        )
    return result


def delete_text_relation_by_predicate_and_text(
    subject_concept_id: str,
    predicate: str,
    text: str,
    *,
    lang: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    garbage_collect: bool = False,
) -> Dict[str, Any]:
    """Delete a text relation by predicate and text value. Optionally garbage-collect orphaned text values.

    Finds the text relation matching the given predicate and text value, then deletes it.
    """
    if not can_access_concept(subject_concept_id):
        raise PermissionError("User cannot delete text for an inaccessible concept")

    lang = (lang or "en").strip() or "en"
    raw_text = _prepare_persisted_text(text)
    fingerprint = _compute_fingerprint(raw_text, lang)

    if predicate == RelationPredicate.HAS_NAME and context:
        name_type = context.get("name_type")
        if isinstance(name_type, str):
            normalized_context = dict(context)
            normalized_context["name_type"] = name_type.strip().upper()
            context = normalized_context

    text_value = TextValuesRepository.find_one(
        {"fingerprint": fingerprint, "lang": lang}
    )
    if not text_value:
        # Legacy fallback: direct text/lang lookup
        text_value = TextValuesRepository.find_one({"text": raw_text, "lang": lang})
    if not text_value:
        text_value = TextValuesRepository.find_one({"text": raw_text})
    if not text_value:
        raise ValueError(f"Text value '{text}' not found")

    text_value_id = str(text_value["_id"])

    rel_candidates = list(
        TextRelationsRepository.find(
            {
                "subject_concept_id": subject_concept_id,
                "predicate": predicate,
                "object_text_id": text_value_id,
            }
        )
    )

    if not rel_candidates:
        raise ValueError(
            f"Text relation not found for predicate '{predicate}' and text '{text}'"
        )

    relation = None
    if context:
        for rel in rel_candidates:
            existing_context = rel.get("context") or {}
            if all(
                existing_context.get(k) == v
                for k, v in context.items()
                if v is not None
            ):
                relation = rel
                break
    if relation is None:
        relation = rel_candidates[0]

    relation_id = str(relation["_id"])

    # Delete the relation
    TextRelationsRepository.delete_one({"_id": ObjectId(relation_id)})
    _invalidate_stats_for_predicate_change(predicate)
    _invalidate_workflow_routing_projection_for_text_relation_change(
        subject_concept_id=subject_concept_id,
        predicate=predicate,
    )

    # Check if the text value is now orphaned
    orphaned = False
    if garbage_collect:
        others = TextRelationsRepository.find_one({"object_text_id": text_value_id})
        if not others:
            # Safe to delete the text value
            TextValuesRepository.delete_one({"_id": ObjectId(text_value_id)})
            orphaned = True

    result = {
        "deleted": True,
        "relation_id": relation_id,
        "predicate": predicate,
        "text": text,
        "orphaned_text_value_deleted": orphaned,
    }
    _emit_text_relation_mutation_event(
        event_type=_EVENT_TYPE_TEXT_RELATION_DELETED,
        subject_concept_id=subject_concept_id,
        relation_id=relation_id,
        predicate=predicate,
        text=raw_text,
        lang=lang,
        extra_payload={"orphaned_text_value_deleted": orphaned},
    )
    return result


def delete_text_relation(
    subject_concept_id: str,
    relation_id: str,
    *,
    garbage_collect: bool = False,
) -> Dict[str, Any]:
    """Delete a specific text relation. Optionally garbage-collect orphaned text values.

    For now, we retain the TextValue (shared across relations) unless unreferenced;
    if unreferenced we remove it to avoid buildup.
    """
    if not can_access_concept(subject_concept_id):
        raise PermissionError("User cannot delete text for an inaccessible concept")
    rel = TextRelationsRepository.find_one({"_id": ObjectId(relation_id)})
    if not rel:
        raise ValueError("Relation not found")
    if rel.get("subject_concept_id") != subject_concept_id:
        raise ValueError("Relation subject mismatch")
    tv_id = rel.get("object_text_id")
    predicate = rel.get("predicate")
    TextRelationsRepository.delete_one({"_id": ObjectId(relation_id)})
    _invalidate_stats_for_predicate_change(
        predicate if isinstance(predicate, str) else None
    )
    _invalidate_workflow_routing_projection_for_text_relation_change(
        subject_concept_id=subject_concept_id,
        predicate=predicate if isinstance(predicate, str) else None,
    )
    orphaned = False
    if tv_id and garbage_collect:
        # Check if any other relation references this text value
        others = TextRelationsRepository.find_one({"object_text_id": tv_id})
        if not others:
            # Safe to delete
            TextValuesRepository.delete_one({"_id": ObjectId(tv_id)})
            orphaned = True
    result = {
        "deleted": True,
        "relation_id": relation_id,
        "orphaned_text_value_deleted": orphaned,
    }
    _emit_text_relation_mutation_event(
        event_type=_EVENT_TYPE_TEXT_RELATION_DELETED,
        subject_concept_id=subject_concept_id,
        relation_id=relation_id,
        predicate=predicate if isinstance(predicate, str) else None,
        extra_payload={"orphaned_text_value_deleted": orphaned},
    )
    return result


def get_text_relations_summary(
    subject_concept_id: str,
    *,
    predicates: Optional[List[str]] = None,
    languages: Optional[List[str]] = None,
    max_relation_ids_per_group: int = 25,
    context_view: TextContextView = "base_publication",
) -> Dict[str, Any]:
    """Return bounded counts and typed row IDs by predicate/language.

    This is intentionally "lightweight": it does not return full text bodies.

    Notes:
    - Text relation documents do not store language directly; language is derived from
      the referenced text_values documents.
    - Base `languages` filtering is applied after resolving text value language.
    - Actor-effective scoped counts are lower bounds when
      ``scoped_query_truncated`` is true.
    """
    if context_view not in _TEXT_CONTEXT_VIEWS:
        raise ValueError(
            "context_view must be 'base_publication' or 'actor_effective'"
        )
    if not can_access_concept(subject_concept_id):
        raise PermissionError("User cannot view text for an inaccessible concept")

    pred_filter: Dict[str, Any] = {
        "subject_concept_id": subject_concept_id,
        "predicate": generic_text_read_predicate_filter(predicates=predicates),
    }

    rels = list(
        TextRelationsRepository.find(
            pred_filter,
            projection={
                "_id": 1,
                "predicate": 1,
                "object_text_id": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        )
    )

    tv_object_ids: List[ObjectId] = []
    tv_string_ids: List[str] = []
    for rel in rels:
        tv_id = rel.get("object_text_id")
        if not tv_id:
            continue
        if isinstance(tv_id, ObjectId):
            tv_object_ids.append(tv_id)
        elif isinstance(tv_id, str):
            try:
                tv_object_ids.append(ObjectId(tv_id))
            except (InvalidId, TypeError):
                tv_string_ids.append(tv_id)

    tv_lang_by_id: Dict[str, str] = {}
    if tv_object_ids:
        for tv in TextValuesRepository.find(
            {"_id": {"$in": tv_object_ids}},
            projection={"lang": 1},
        ):
            tv_lang_by_id[str(tv.get("_id"))] = str(tv.get("lang") or "")
    if tv_string_ids:
        # Some stored ids may be non-ObjectId strings.
        for tv in TextValuesRepository.find(
            {"_id": {"$in": tv_string_ids}},
            projection={"lang": 1},
        ):
            tv_lang_by_id[str(tv.get("_id"))] = str(tv.get("lang") or "")

    language_allow = None
    if languages:
        language_allow = {str(lang) for lang in languages if isinstance(lang, str)}

    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for rel in rels:
        predicate = str(rel.get("predicate") or "")
        tv_id = rel.get("object_text_id")
        tv_id_str = str(tv_id) if tv_id is not None else ""
        lang = tv_lang_by_id.get(tv_id_str) or ""

        if language_allow is not None and lang not in language_allow:
            continue

        key = (predicate, lang)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "predicate": predicate,
                "language": lang,
                "count": 0,
                "relation_ids": [],
                "assertion_ids": [],
                "latest_relation_id": None,
                "latest_assertion_id": None,
                "latest_row_kind": None,
                "latest_updated_at": None,
            }
            grouped[key] = bucket

        bucket["count"] += 1
        rel_id = str(rel.get("_id"))
        if rel_id:
            if len(bucket["relation_ids"]) < max_relation_ids_per_group:
                bucket["relation_ids"].append(rel_id)

        updated_at = _iso_utc(rel.get("updated_at") or rel.get("created_at"))
        if updated_at:
            current_latest = bucket.get("latest_updated_at")
            if current_latest is None or updated_at > current_latest:
                bucket["latest_updated_at"] = updated_at
                bucket["latest_relation_id"] = rel_id
                bucket["latest_assertion_id"] = None
                bucket["latest_row_kind"] = "base_text_relation"

    scoped_assertions: List[Dict[str, Any]] = []
    scoped_query_truncated = False
    scoped_visibility_filtered = False
    scoped_counts_are_lower_bounds = False
    if context_view == "actor_effective":
        from .scoped_assertion_service import (
            list_visible_scoped_assertions_page,
        )

        requested_scoped_predicates = [
            str(item).strip()
            for item in predicates or ()
            if isinstance(item, str) and str(item).strip()
        ]
        scoped_predicates = [
            item
            for item in requested_scoped_predicates
            if not is_hidden_from_generic_text_reads(item)
        ]
        if not requested_scoped_predicates or scoped_predicates:
            scoped_page = list_visible_scoped_assertions_page(
                subject_concept_ids=[subject_concept_id],
                predicates=scoped_predicates or None,
                object_kind="text",
                languages=languages,
                limit=1000,
            )
            scoped_assertions = [
                assertion
                for assertion in scoped_page.get("items") or ()
                if not is_hidden_from_generic_text_reads(
                    assertion.get("predicate")
                )
            ]
            scoped_query_truncated = bool(scoped_page.get("truncated"))
            scoped_visibility_filtered = bool(
                scoped_page.get("visibility_filtered")
            )
            scoped_counts_are_lower_bounds = bool(
                scoped_page.get("counts_are_lower_bounds")
            )
    for assertion in scoped_assertions:
        object_text = assertion.get("object_text")
        if not isinstance(object_text, dict):
            continue
        predicate = str(assertion.get("predicate") or "")
        lang = str(object_text.get("language") or "")
        if language_allow is not None and lang not in language_allow:
            continue
        key = (predicate, lang)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "predicate": predicate,
                "language": lang,
                "count": 0,
                "relation_ids": [],
                "assertion_ids": [],
                "latest_relation_id": None,
                "latest_assertion_id": None,
                "latest_row_kind": None,
                "latest_updated_at": None,
            }
            grouped[key] = bucket
        bucket["count"] += 1
        bucket["scoped_count"] = int(bucket.get("scoped_count") or 0) + 1
        assertion_id = str(assertion.get("assertion_id") or "")
        if assertion_id and len(bucket["assertion_ids"]) < max_relation_ids_per_group:
            bucket["assertion_ids"].append(assertion_id)
        updated_at = _iso_utc(
            assertion.get("updated_at") or assertion.get("created_at")
        )
        current_latest = bucket.get("latest_updated_at")
        if updated_at and (current_latest is None or updated_at > current_latest):
            bucket["latest_updated_at"] = updated_at
            bucket["latest_relation_id"] = None
            bucket["latest_assertion_id"] = assertion_id
            bucket["latest_row_kind"] = "scoped_assertion"

    summary_items = list(grouped.values())
    summary_items.sort(
        key=lambda x: (x.get("predicate") or "", x.get("language") or "")
    )
    return {
        "success": True,
        "concept_id": subject_concept_id,
        "context_view": context_view,
        "groups": summary_items,
        "groups_found": len(summary_items),
        "total_relations_scanned": len(rels) + len(scoped_assertions),
        "scoped_relations_scanned": len(scoped_assertions),
        "scoped_query_truncated": scoped_query_truncated,
        "scoped_visibility_filtered": scoped_visibility_filtered,
        "counts_are_lower_bounds": scoped_counts_are_lower_bounds,
        "max_relation_ids_per_group": max_relation_ids_per_group,
    }


def upsert_singleton_text_relation(
    *,
    subject_concept_id: str,
    predicate: str,
    text: str,
    lang: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    garbage_collect: bool = True,
) -> Dict[str, Any]:
    """Upsert text and enforce singleton semantics for (concept, predicate, language).

    `policy='replace_others'` means: after inserting/upserting the desired relation,
    delete all other relations for the same subject+predicate whose text_value language
    matches `lang`.
    """
    if policy != "replace_others":
        raise ValueError("Unsupported policy (expected 'replace_others')")

    result = upsert_text_for_concept(
        subject_concept_id=subject_concept_id,
        predicate=predicate,
        text=text,
        lang=lang,
        provenance=provenance,
        context=context,
    )
    kept_relation_id = str(result.get("relation_id") or "")

    rels = list(
        TextRelationsRepository.find(
            {
                "subject_concept_id": subject_concept_id,
                "predicate": predicate,
            },
            projection={"_id": 1, "object_text_id": 1},
        )
    )

    tv_object_ids: List[ObjectId] = []
    tv_ids_raw: List[str] = []
    for rel in rels:
        tv_id = rel.get("object_text_id")
        if tv_id is None:
            continue
        if isinstance(tv_id, ObjectId):
            tv_object_ids.append(tv_id)
        else:
            tv_id_str = str(tv_id)
            try:
                tv_object_ids.append(ObjectId(tv_id_str))
            except (InvalidId, TypeError):
                tv_ids_raw.append(tv_id_str)

    tv_lang_by_id: Dict[str, str] = {}
    if tv_object_ids:
        for tv in TextValuesRepository.find(
            {"_id": {"$in": tv_object_ids}},
            projection={"lang": 1},
        ):
            tv_lang_by_id[str(tv.get("_id"))] = str(tv.get("lang") or "")
    if tv_ids_raw:
        for tv in TextValuesRepository.find(
            {"_id": {"$in": tv_ids_raw}},
            projection={"lang": 1},
        ):
            tv_lang_by_id[str(tv.get("_id"))] = str(tv.get("lang") or "")

    replaced_relation_ids: List[str] = []
    lang_normalised = (lang or "").strip()
    for rel in rels:
        rel_id = str(rel.get("_id"))
        if not rel_id or rel_id == kept_relation_id:
            continue

        tv_id = rel.get("object_text_id")
        tv_id_str = str(tv_id) if tv_id is not None else ""
        rel_lang = tv_lang_by_id.get(tv_id_str) or ""

        if rel_lang != lang_normalised:
            continue

        delete_text_relation(
            subject_concept_id,
            rel_id,
            garbage_collect=garbage_collect,
        )
        replaced_relation_ids.append(rel_id)

    return {
        "success": True,
        "concept_id": subject_concept_id,
        "predicate": predicate,
        "language": lang,
        "kept_relation_id": kept_relation_id,
        "replaced_relation_ids": replaced_relation_ids,
        "replaced_count": len(replaced_relation_ids),
        "relation_created": bool(result.get("relation_created")),
        "text_value_id": result.get("text_value_id"),
    }


def audit_concept_text_relations(
    concept_id: str,
    *,
    include_text_preview: bool = True,
    max_preview_length: int = 100,
) -> Dict[str, Any]:
    """Audit all text relations attached to a concept, including accessibility status.

    This function bypasses normal access control to provide a complete view of all
    text relations. It's designed for pre-rename auditing to identify relations that
    would block a rename operation.

    Returns:
        A dictionary with:
        - concept_id: The queried concept
        - total_relations: Total count of text relations
        - accessible_count: Relations that can be modified by current user
        - inaccessible_count: Relations that would block rename
        - relations: List of relation details with accessibility status
        - summary: Grouped counts by predicate
    """
    # Query all text relations directly, bypassing normal access control
    rels = list(
        TextRelationsRepository.find(
            {
                "subject_concept_id": concept_id,
                "predicate": generic_text_read_predicate_filter(),
            },
            limit=1000,
        )
    )
    rels = [
        rel
        for rel in rels
        if not is_hidden_from_generic_text_reads(rel.get("predicate"))
    ]

    if not rels:
        return {
            "success": True,
            "concept_id": concept_id,
            "total_relations": 0,
            "accessible_count": 0,
            "inaccessible_count": 0,
            "relations": [],
            "summary": {},
            "can_rename": True,
            "blocking_relations": [],
        }

    # Collect text value IDs to fetch text content
    tv_ids: List[ObjectId] = []
    for rel in rels:
        tv_id = rel.get("object_text_id")
        if tv_id:
            try:
                if isinstance(tv_id, ObjectId):
                    tv_ids.append(tv_id)
                elif isinstance(tv_id, str):
                    tv_ids.append(ObjectId(tv_id))
            except (InvalidId, TypeError):
                pass

    # Fetch text values
    tv_by_id: Dict[str, Dict[str, Any]] = {}
    if tv_ids:
        for tv in TextValuesRepository.find({"_id": {"$in": tv_ids}}):
            tv_by_id[str(tv.get("_id"))] = {
                "text": tv.get("text", ""),
                "lang": tv.get("lang", ""),
            }

    # Check accessibility for each relation
    accessible_count = 0
    inaccessible_count = 0
    relations_detail: List[Dict[str, Any]] = []
    blocking_relations: List[Dict[str, Any]] = []
    summary_by_predicate: Dict[str, Dict[str, int]] = {}

    for rel in rels:
        rel_id = str(rel.get("_id"))
        predicate = rel.get("predicate", "")
        tv_id = rel.get("object_text_id")
        tv_id_str = str(tv_id) if tv_id else ""

        # Check if current user can access this concept (and thus modify relations)
        is_accessible = can_access_concept(concept_id)

        if is_accessible:
            accessible_count += 1
        else:
            inaccessible_count += 1

        # Get text info
        tv_info = tv_by_id.get(tv_id_str, {})
        text = tv_info.get("text", "")
        lang = tv_info.get("lang", "")

        # Build relation detail
        rel_detail: Dict[str, Any] = {
            "relation_id": rel_id,
            "predicate": predicate,
            "text_value_id": tv_id_str,
            "language": lang,
            "accessible": is_accessible,
            "context": rel.get("context", {}),
            "created_at": (
                rel.get("created_at").isoformat() if rel.get("created_at") else None
            ),
        }

        if include_text_preview:
            preview = text[:max_preview_length]
            if len(text) > max_preview_length:
                preview += "..."
            rel_detail["text_preview"] = preview

        relations_detail.append(rel_detail)

        if not is_accessible:
            blocking_relations.append(rel_detail)

        # Update summary
        if predicate not in summary_by_predicate:
            summary_by_predicate[predicate] = {"accessible": 0, "inaccessible": 0}
        if is_accessible:
            summary_by_predicate[predicate]["accessible"] += 1
        else:
            summary_by_predicate[predicate]["inaccessible"] += 1

    return {
        "success": True,
        "concept_id": concept_id,
        "total_relations": len(rels),
        "accessible_count": accessible_count,
        "inaccessible_count": inaccessible_count,
        "can_rename": inaccessible_count == 0,
        "relations": relations_detail,
        "blocking_relations": blocking_relations,
        "summary": summary_by_predicate,
    }


__all__ = [
    "RelationPredicate",
    "create_text_value",
    "link_text_to_concept",
    "upsert_text_for_concept",
    "upsert_singleton_text_relation",
    "get_texts_for_concept",
    "get_texts_for_concepts",
    "get_text_relations_summary",
    "update_text_relation_text",
    "delete_text_relation_by_predicate_and_text",
    "delete_text_relation",
    "audit_concept_text_relations",
]
