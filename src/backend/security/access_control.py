"""Per-request helpers enforcing user-specific concept visibility."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bson import BSON
from flask import g, has_request_context, request, session

from ..db.mongo_client import get_concepts_collection
from .visibility_predicates import (
    SPECIFIC_TO_ORG_PREDICATES_READ,
    SPECIFIC_TO_USER_PREDICATES,
    get_specific_to_org_values,
    get_specific_to_user_values,
)
from .authentication_assurance import (
    session_has_required_authentication_assurance,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context state
# ---------------------------------------------------------------------------
_MANUAL_USER: ContextVar[Optional[str]] = ContextVar("access_manual_user", default=None)
_MANUAL_ORG: ContextVar[Optional[str]] = ContextVar("access_manual_org", default=None)
_MANUAL_ACTOR_BOUND: ContextVar[bool] = ContextVar(
    "access_manual_actor_bound",
    default=False,
)
_BYPASS: ContextVar[bool] = ContextVar("access_bypass", default=False)
_FORCE_ENFORCEMENT: ContextVar[bool] = ContextVar(
    "access_force_enforcement",
    default=False,
)
_EVALUATOR: ContextVar["AccessEvaluator | None"] = ContextVar(
    "access_evaluator", default=None
)

_REMOVE = object()

_ACCESS_ID_QUERY_MAX_COUNT = 10_000
_ACCESS_ID_QUERY_MAX_BSON_BYTES = 8 * 1024 * 1024

TRUSTED_IN_PROCESS_ACTOR_SOURCE = "trusted_in_process_actor"
AUTHENTICATED_SESSION_ACTOR_SOURCE = "authenticated_session"
AUTHENTICATED_SESSION_DERIVED_ACTOR_SOURCE = "authenticated_session_derived"
LEGACY_IDENTITY_HEADER_ACTOR_SOURCE = "legacy_identity_header"


def _normalise_concept_id(value: Any) -> Optional[str]:
    """Return a normalised concept id if the value resembles one."""
    if isinstance(value, str):
        candidate = value.strip()
        if candidate and candidate.startswith("#"):
            return candidate
    return None


def _normalise_stored_org_concept_id(value: Any) -> Optional[str]:
    """Return a concept id for org values stored either as #V# ids or slugs."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    return candidate if candidate.startswith("#") else f"#V#{candidate}"


def _bounded_access_id_query_batches(concept_ids: Iterable[str]) -> Iterable[list[str]]:
    """Yield deterministic ``$in`` batches below count and BSON safety bounds."""

    def split_to_bson_budget(batch: list[str]) -> Iterable[list[str]]:
        if len(batch) <= 1:
            yield batch
            return
        encoded_size = len(BSON.encode({"concept_id": {"$in": batch}}))
        if encoded_size <= _ACCESS_ID_QUERY_MAX_BSON_BYTES:
            yield batch
            return
        midpoint = len(batch) // 2
        yield from split_to_bson_budget(batch[:midpoint])
        yield from split_to_bson_budget(batch[midpoint:])

    ordered_ids = sorted(concept_ids)
    for offset in range(0, len(ordered_ids), _ACCESS_ID_QUERY_MAX_COUNT):
        yield from split_to_bson_budget(
            ordered_ids[offset : offset + _ACCESS_ID_QUERY_MAX_COUNT]
        )


def _specific_allows_user(spec: Any, user_id: Optional[str]) -> bool:
    """Decide whether a specific_to_user value permits the current user."""
    if spec is None:
        return True
    if isinstance(spec, list):
        if not spec:
            return True
        if user_id:
            return any(_normalise_concept_id(v) == user_id for v in spec)
        return False
    if isinstance(spec, str):
        cleaned = _normalise_concept_id(spec)
        if cleaned is None:
            return True
        return cleaned == user_id if user_id else False
    # Unknown structure – default to visible to avoid over-zealous hiding.
    return True


def _get_specific_to_user_values(relationships: Dict[str, Any]) -> List[Any]:
    """Compatibility wrapper used by routes/services importing this private helper."""
    return list(get_specific_to_user_values(relationships))


def _specific_allows_actor(spec: Any, actor_id: Optional[str]) -> bool:
    return _specific_allows_user(spec, actor_id)


def _document_visible_to_actor(
    doc: Dict[str, Any],
    user_id: Optional[str],
    org_id: Optional[str],
) -> bool:
    relationships = doc.get("relationships") if isinstance(doc, dict) else None
    if not isinstance(relationships, dict):
        return True

    user_specific = _get_specific_to_user_values(relationships)
    org_specific = list(get_specific_to_org_values(relationships))
    if not user_specific and not org_specific:
        return True
    if user_specific and _specific_allows_actor(user_specific, user_id):
        return True
    if org_specific and _specific_allows_actor(org_specific, org_id):
        return True
    return False


def _document_visible_to_user(doc: Dict[str, Any], user_id: Optional[str]) -> bool:
    """Compatibility wrapper for older callers that only pass user context."""

    return _document_visible_to_actor(doc, user_id, None)


class AccessEvaluator:
    """Cache per-request visibility decisions for related concept identifiers."""

    def __init__(
        self,
        user_id: Optional[str],
        org_id: Optional[str],
        enforce: bool,
    ) -> None:
        self.user_id = user_id
        self.org_id = org_id
        self.enforce = enforce
        self._cache: Dict[str, bool] = {}
        self._collection = None

    def can_access(self, concept_id: Any) -> bool:
        if not self.enforce:
            return True
        normalised = _normalise_concept_id(concept_id)
        if normalised is None:
            return True

        # Virtual concepts that are registered as code-handled predicates should be
        # considered visible, even when no MongoDB concept document exists.
        # This supports UI navigation and text-relations (e.g., adding hasName).
        try:
            from ..vontology.virtual_concept_providers import (  # local import avoids cycles
                is_virtual_concept_id,
            )

            if is_virtual_concept_id(normalised):
                self._cache[normalised] = True
                return True
        except Exception:
            pass

        if normalised == self.user_id:
            self._cache[normalised] = True
            return True
        cached = self._cache.get(normalised)
        if cached is not None:
            return cached
        coll = (
            self._collection
            if self._collection is not None
            else get_concepts_collection()
        )
        self._collection = coll
        if coll is None:
            allowed = True
        else:
            doc = coll.find_one(
                {"concept_id": normalised},
                {
                    **{f"relationships.{p}": 1 for p in SPECIFIC_TO_USER_PREDICATES},
                    **{
                        f"relationships.{p}": 1 for p in SPECIFIC_TO_ORG_PREDICATES_READ
                    },
                },
            )
            if doc:
                rels = doc.get("relationships", {})
                user_specific = _get_specific_to_user_values(rels) if rels else None
                org_specific = get_specific_to_org_values(rels) if rels else None
                if user_specific:
                    # Log user-specific concept access
                    _log.info(
                        f"[access_filter] concept_id={normalised} specific_to_user={user_specific} authenticated_user={self.user_id}"
                    )
                if org_specific:
                    _log.info(
                        f"[access_filter] concept_id={normalised} specific_to_org={org_specific} authenticated_org={self.org_id}"
                    )
                allowed = _document_visible_to_actor(
                    doc,
                    self.user_id,
                    self.org_id,
                )
            else:
                allowed = False
        self._cache[normalised] = allowed
        return allowed

    def accessible_concept_ids(self, concept_ids: Iterable[Any]) -> set[str]:
        """Return accessible concept ids using the same semantics as can_access."""

        normalised_ids = {
            normalised
            for concept_id in concept_ids
            if (normalised := _normalise_concept_id(concept_id)) is not None
        }
        if not normalised_ids:
            return set()
        if not self.enforce:
            for concept_id in normalised_ids:
                self._cache[concept_id] = True
            return set(normalised_ids)

        allowed: set[str] = set()
        uncached: set[str] = set()
        for concept_id in normalised_ids:
            cached = self._cache.get(concept_id)
            if cached is True:
                allowed.add(concept_id)
                continue
            if cached is False:
                continue
            if concept_id == self.user_id:
                self._cache[concept_id] = True
                allowed.add(concept_id)
                continue
            uncached.add(concept_id)

        if uncached:
            code_concepts: set[str] = set()
            try:
                from ..vontology.virtual_concept_providers import is_virtual_concept_id

                for concept_id in tuple(uncached):
                    if is_virtual_concept_id(concept_id):
                        code_concepts.add(concept_id)
            except Exception:
                code_concepts = set()
            for concept_id in code_concepts:
                self._cache[concept_id] = True
                allowed.add(concept_id)
                uncached.discard(concept_id)

        if not uncached:
            return allowed

        coll = (
            self._collection
            if self._collection is not None
            else get_concepts_collection()
        )
        self._collection = coll
        if coll is None:
            for concept_id in uncached:
                self._cache[concept_id] = True
            return allowed | uncached

        projection = {
            "concept_id": 1,
            **{f"relationships.{p}": 1 for p in SPECIFIC_TO_USER_PREDICATES},
            **{f"relationships.{p}": 1 for p in SPECIFIC_TO_ORG_PREDICATES_READ},
        }
        seen: set[str] = set()
        for concept_id_batch in _bounded_access_id_query_batches(uncached):
            cursor: Any = coll.find(
                {"concept_id": {"$in": concept_id_batch}},
                projection,
            )
            set_batch_size = getattr(cursor, "batch_size", None)
            if callable(set_batch_size):
                cursor = set_batch_size(min(len(concept_id_batch), 20_000))
            for doc in cursor:
                if not isinstance(doc, dict):
                    continue
                concept_id = _normalise_concept_id(doc.get("concept_id"))
                if concept_id is None:
                    continue
                seen.add(concept_id)
                is_visible = _document_visible_to_actor(doc, self.user_id, self.org_id)
                self._cache[concept_id] = is_visible
                if is_visible:
                    allowed.add(concept_id)

        for concept_id in uncached - seen:
            self._cache[concept_id] = False
        return allowed


_HEADER_CACHE: ContextVar[Optional[str]] = ContextVar(
    "access_header_user", default=None
)


def _validate_person_concept(concept_id: Optional[str]) -> Optional[str]:
    if not concept_id:
        return None
    norm = _normalise_concept_id(concept_id)
    if not norm:
        return None
    try:
        from ..services.concept_service import (
            _find_raw_concept_by_exact_concept_id,
        )  # local import to avoid cycles

        # Header/session identity validation should be a cheap exact lookup, not a
        # recursive alias/name-resolution path. Re-entering access-controlled name
        # resolution here can amplify per-request identity checks into deep recursion
        # and make authenticated UI routes hang before they reach their own logic.
        with bypass_access_control():
            doc = _find_raw_concept_by_exact_concept_id(norm)
        if not doc:
            return None

        # Allow the canonical von_user type itself
        if norm == "#V#von_user":
            return norm

        relationships = doc.get("relationships") if isinstance(doc, dict) else None
        if isinstance(relationships, dict):
            instance_edges = []
            for key in (
                "is_an_instance_of",
                "instance_of",
                "is_an_instance_of",
                "type",
            ):  # tolerate legacy keys
                val = relationships.get(key)
                if isinstance(val, list):
                    instance_edges.extend(val)
                elif isinstance(val, str):
                    instance_edges.append(val)
            if any(
                _normalise_concept_id(edge) in {"#V#person", "#V#von_user"}
                for edge in instance_edges
            ):
                return norm

        direct_name = doc.get("direct_concept_name") or doc.get("concept_type")
        if isinstance(direct_name, str) and direct_name.lower().startswith("person"):
            return norm

        # Check for indirect instance relationships through type hierarchies
        # Logic: A is instance of B AND B is subtype of C → A is instance of C
        from ..vontology.utils_vontology import (
            get_vontology_node_and_ancestor_instance_ids,
        )

        if get_vontology_node_and_ancestor_instance_ids(norm):
            return norm

    except Exception:
        return None
    return None


def get_effective_user_concept_id_with_source() -> tuple[str | None, str | None]:
    """Resolve the effective actor and identify how that identity was obtained.

    Legacy identity headers remain a compatibility input for actor-scoped reads.
    Callers that authorise effects must inspect the source instead of treating a
    validated person concept as proof of authentication.
    """

    manual = _MANUAL_USER.get()
    if _MANUAL_ACTOR_BOUND.get():
        if manual is None:
            return None, None
        return manual, TRUSTED_IN_PROCESS_ACTOR_SOURCE
    if manual is not None:
        return manual, TRUSTED_IN_PROCESS_ACTOR_SOURCE
    if not has_request_context():
        return None, None

    if not session_has_required_authentication_assurance(session):
        _log.warning("Rejected pre-cutover or unassured email-authenticated session")
        return None, None

    # Primary authority is the server-side session which is populated during the
    # authenticated login flow.
    session_user = _normalise_concept_id(session.get("user_concept_id"))
    if session_user:
        return session_user, AUTHENTICATED_SESSION_ACTOR_SOURCE

    # Fallback: derive a candidate concept id from session user_id/email when
    # user_concept_id is missing (e.g. older login flow or partial session).
    session_slug = session.get("user_id") or session.get("user_email")
    if isinstance(session_slug, str) and session_slug.strip():
        slug = session_slug.strip()
        if "@" in slug:
            slug = slug.split("@", 1)[0]
        candidate = f"#V#{slug}" if not slug.startswith("#V#") else slug
        candidate = _normalise_concept_id(candidate)
        if candidate:
            validated = _validate_person_concept(candidate)
            if validated:
                return validated, AUTHENTICATED_SESSION_DERIVED_ACTOR_SOURCE

    cached_header = getattr(g, "_von_access_header_user", None)
    if cached_header:
        return cached_header, LEGACY_IDENTITY_HEADER_ACTOR_SOURCE

    # Allow a guarded per-request override via headers for trusted automation
    # clients (legacy behaviour relied on this pathway). We validate that the
    # supplied identifier maps to an existing person concept before accepting it.
    header_user = request.headers.get("X-User-Concept-ID") or request.headers.get(
        "X-User-Client-ID"
    )
    header_user = _normalise_concept_id(header_user)
    if header_user:
        validated = _validate_person_concept(header_user)
        if validated:
            g._von_access_header_user = validated
            return validated, LEGACY_IDENTITY_HEADER_ACTOR_SOURCE
    return None, None


def get_effective_user_concept_id() -> Optional[str]:
    """Return the effective actor, retaining legacy header support for reads."""

    actor_id, _source = get_effective_user_concept_id_with_source()
    return actor_id


def get_effective_organisation_concept_id() -> Optional[str]:
    manual = _MANUAL_ORG.get()
    if _MANUAL_ACTOR_BOUND.get() or manual is not None:
        return manual
    if not has_request_context():
        return None
    if not session_has_required_authentication_assurance(session):
        return None
    try:
        window_session_id = request.headers.get("X-Von-Window-Session")
        if window_session_id:
            from ..services.window_session_context_service import (
                WindowSessionContextUnavailable,
                get_effective_context,
            )

            user_for_context = get_effective_user_concept_id() or session.get(
                "user_concept_id"
            )
            effective = get_effective_context(
                window_session_id,
                dict(session),
                user_for_context,
                require_known_window=True,
            )
            # A known window session is authoritative even when its explicit
            # selection is Personal. Falling through here would resurrect the
            # browser-wide Flask organisation selected by another tab.
            return _normalise_stored_org_concept_id(
                effective.get("organisation_id")
            )
    except WindowSessionContextUnavailable:
        # An explicit unknown/expired/other-actor selector must never inherit
        # the browser-wide organisation selected by another tab.
        return None
    except Exception:
        pass
    for key in ("organisation_concept_id", "org_concept_id", "org_id"):
        candidate = _normalise_stored_org_concept_id(session.get(key))
        if candidate:
            return candidate
    return None


def is_bypass_enabled() -> bool:
    return _BYPASS.get()


def should_enforce_access_control() -> bool:
    if is_bypass_enabled():
        return False
    if _FORCE_ENFORCEMENT.get():
        return True
    if (
        _MANUAL_ACTOR_BOUND.get()
        or _MANUAL_USER.get() is not None
        or _MANUAL_ORG.get() is not None
    ):
        return True
    return has_request_context()


def _current_evaluator() -> AccessEvaluator:
    user_id = get_effective_user_concept_id()
    org_id = get_effective_organisation_concept_id()
    enforce = should_enforce_access_control()
    if has_request_context():
        current = getattr(g, "_von_access_evaluator", None)
        if (
            current is None
            or current.user_id != user_id
            or current.org_id != org_id
            or current.enforce != enforce
        ):
            current = AccessEvaluator(user_id, org_id, enforce)
            g._von_access_evaluator = current
        return current

    current = _EVALUATOR.get()
    if (
        current is None
        or current.user_id != user_id
        or current.org_id != org_id
        or current.enforce != enforce
    ):
        current = AccessEvaluator(user_id, org_id, enforce)
        _EVALUATOR.set(current)
    return current


def describe_concept_access(concept_id: Any) -> Dict[str, Any]:
    """Return safe access diagnostics for exact concept access decisions."""

    user_id = get_effective_user_concept_id()
    org_id = get_effective_organisation_concept_id()
    normalised = _normalise_concept_id(concept_id)
    details: Dict[str, Any] = {
        "concept_id": normalised or concept_id,
        "authenticated_user_concept_id": user_id,
        "organisation_concept_id": org_id,
        "access_control_enforced": should_enforce_access_control(),
        "visibility_predicate_families": ["specific_to_user", "specific_to_org"],
    }
    if not should_enforce_access_control():
        details.update(
            {
                "exists": None,
                "accessible": True,
                "restriction_families_present": [],
            }
        )
        return details
    if normalised is None:
        details.update(
            {
                "exists": True,
                "accessible": True,
                "restriction_families_present": [],
            }
        )
        return details

    try:
        from ..vontology.virtual_concept_providers import is_virtual_concept_id

        if is_virtual_concept_id(normalised):
            details.update(
                {
                    "exists": True,
                    "accessible": True,
                    "restriction_families_present": [],
                }
            )
            return details
    except Exception:
        pass

    coll = get_concepts_collection()
    if coll is None:
        details.update(
            {
                "exists": None,
                "accessible": True,
                "restriction_families_present": [],
            }
        )
        return details

    doc = coll.find_one(
        {"concept_id": normalised},
        {
            **{f"relationships.{p}": 1 for p in SPECIFIC_TO_USER_PREDICATES},
            **{f"relationships.{p}": 1 for p in SPECIFIC_TO_ORG_PREDICATES_READ},
        },
    )
    if not isinstance(doc, dict):
        details.update(
            {
                "exists": False,
                "accessible": False,
                "restriction_families_present": [],
            }
        )
        return details

    relationships = doc.get("relationships")
    relationships = relationships if isinstance(relationships, dict) else {}
    user_specific = _get_specific_to_user_values(relationships)
    org_specific = list(get_specific_to_org_values(relationships))
    restriction_families = []
    if user_specific:
        restriction_families.append("specific_to_user")
    if org_specific:
        restriction_families.append("specific_to_org")
    details.update(
        {
            "exists": True,
            "accessible": _document_visible_to_actor(doc, user_id, org_id),
            "restriction_families_present": restriction_families,
            "specific_to_user_restricted": bool(user_specific),
            "specific_to_org_restricted": bool(org_specific),
        }
    )
    return details


def _field_has_no_visibility_restriction(field: str) -> Dict[str, Any]:
    return {
        "$or": [
            {field: {"$exists": False}},
            {field: {"$eq": None}},
            {field: {"$size": 0}},
        ]
    }


def build_visibility_filter() -> Optional[Dict[str, Any]]:
    if not should_enforce_access_control():
        _log.info("[access_filter] Access control disabled - no filtering applied")
        return None
    user_id = get_effective_user_concept_id()

    user_org_id = get_effective_organisation_concept_id()

    # Get user email for detailed logging
    user_email = "unknown"
    try:
        if has_request_context():
            user_email = session.get("user_email", "no_email_in_session")
    except Exception:
        pass

    _log.info(
        f"[access_filter] Building visibility filter - authenticated_user={user_id} org={user_org_id} email={user_email}"
    )

    # Query/list visibility mirrors exact access checks: globally visible when
    # no supported user/org visibility predicate restricts the document, or
    # visible when any current actor component matches a restriction.
    all_visibility_predicates = (
        *SPECIFIC_TO_USER_PREDICATES,
        *SPECIFIC_TO_ORG_PREDICATES_READ,
    )
    clauses: List[Dict[str, Any]] = [
        {
            "$and": [
                _field_has_no_visibility_restriction(f"relationships.{predicate}")
                for predicate in all_visibility_predicates
            ]
        }
    ]

    # User-specific visibility: user appears in ANY of the predicate variants
    if user_id:
        for predicate in SPECIFIC_TO_USER_PREDICATES:
            clauses.append({f"relationships.{predicate}": {"$in": [user_id]}})
        _log.info(
            f"[access_filter] Including user-specific concepts for user={user_id}"
        )
    else:
        # Instrumentation: no effective user; user-specific concepts will be hidden.
        _log.info(
            "[access_filter] No authenticated user - user-specific concepts will be hidden"
        )
        try:
            _log.info(
                "[access_filter] Context details - path=%s session_user=%s",
                request.path if has_request_context() else None,
                session.get("user_concept_id") if has_request_context() else None,
            )
        except Exception:
            pass

    # Organisation-specific visibility (Phase 1)
    if user_org_id:
        for predicate in SPECIFIC_TO_ORG_PREDICATES_READ:
            clauses.append({f"relationships.{predicate}": {"$in": [user_org_id]}})
        _log.info(
            f"[access_filter] Including org-specific concepts for org={user_org_id}"
        )

    return {"$or": clauses}


def apply_concept_query_filter(base_filter: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    visibility = build_visibility_filter()
    base = dict(base_filter) if base_filter else {}
    if not visibility:
        return base
    if not base:
        return visibility
    return {"$and": [base, visibility]}


def apply_pipeline_filter(pipeline: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    visibility = build_visibility_filter()
    pipeline_list = list(pipeline) if pipeline else []
    if not visibility:
        return pipeline_list
    match_stage = {"$match": visibility}
    if pipeline_list:
        first_stage = pipeline_list[0]
        if isinstance(first_stage, dict) and any(
            k in first_stage for k in ("$geoNear", "$search", "$vectorSearch")
        ):
            pipeline_list.insert(1, match_stage)
            return pipeline_list
        if "$match" in first_stage:
            combined = {"$and": [first_stage["$match"], visibility]}
            pipeline_list[0] = {"$match": combined}
            return pipeline_list
    pipeline_list.insert(0, match_stage)
    return pipeline_list


def _sanitize_relationship_value(
    value: Any, evaluator: AccessEvaluator
) -> Tuple[Any, bool]:
    if isinstance(value, list):
        new_list: List[Any] = []
        changed = False
        for item in value:
            sanitised, item_changed = _sanitize_relationship_value(item, evaluator)
            if sanitised is _REMOVE:
                changed = True
                continue
            if item_changed:
                changed = True
            new_list.append(sanitised)
        return new_list, changed or len(new_list) != len(value)
    if isinstance(value, dict):
        new_dict: Dict[Any, Any] = {}
        changed = False
        for key, val in value.items():
            sanitised, item_changed = _sanitize_relationship_value(val, evaluator)
            if sanitised is _REMOVE:
                changed = True
                continue
            if item_changed:
                changed = True
            new_dict[key] = sanitised
        return new_dict, changed
    normalised = _normalise_concept_id(value)
    if normalised is not None and not evaluator.can_access(normalised):
        return _REMOVE, True
    return value, False


def _relationship_concept_ids(value: Any) -> set[str]:
    """Collect relationship targets for one batch visibility preflight.

    Sanitisation still delegates the final decision for every value to the
    request-local evaluator.  Pre-populating that evaluator avoids issuing one
    MongoDB query per relationship target while iterating large concept
    cursors, which can otherwise turn workflow discovery into an unbounded
    sequence of remote reads.
    """

    concept_ids: set[str] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
            continue
        if isinstance(item, dict):
            pending.extend(item.values())
            continue
        normalised = _normalise_concept_id(item)
        if normalised is not None:
            concept_ids.add(normalised)
    return concept_ids


def prewarm_concept_relationship_access(
    documents: Iterable[Dict[str, Any]],
) -> AccessEvaluator | None:
    """Batch-cache visibility for relationship targets in concept documents."""

    if not should_enforce_access_control():
        return None
    evaluator = _current_evaluator()
    concept_ids: set[str] = set()
    for document in documents:
        if not isinstance(document, dict):
            continue
        relationships = document.get("relationships")
        if isinstance(relationships, dict):
            concept_ids.update(_relationship_concept_ids(relationships))
    if concept_ids:
        evaluator.accessible_concept_ids(concept_ids)
    return evaluator


def sanitize_concept_document(
    doc: Optional[Dict[str, Any]],
    *,
    access_evaluator: AccessEvaluator | None = None,
) -> Optional[Dict[str, Any]]:
    if doc is None:
        return None
    if not should_enforce_access_control():
        return doc
    evaluator = access_evaluator or _current_evaluator()
    user_id = evaluator.user_id

    # Get user email for detailed logging
    user_email = "unknown"
    try:
        if has_request_context():
            user_email = session.get("user_email", "no_email_in_session")
    except Exception:
        pass

    concept_id = doc.get("concept_id", "unknown_concept")
    relationships = doc.get("relationships")
    relationships = relationships if isinstance(relationships, dict) else {}
    user_specific = _get_specific_to_user_values(relationships)

    org_id = evaluator.org_id
    org_specific = get_specific_to_org_values(relationships)

    if not _document_visible_to_actor(doc, user_id, org_id):
        _log.info(
            f"[access_filter] BLOCKED concept_id={concept_id} specific_to_user={user_specific} specific_to_org={org_specific} authenticated_user={user_id} authenticated_org={org_id} email={user_email}"
        )
        return None

    # Log if this is a user-specific concept that was allowed
    if user_specific:
        _log.info(
            f"[access_filter] ALLOWED concept_id={concept_id} specific_to_user={user_specific} authenticated_user={user_id} email={user_email}"
        )
    if org_specific:
        _log.info(
            f"[access_filter] ALLOWED concept_id={concept_id} specific_to_org={org_specific} authenticated_org={org_id} email={user_email}"
        )
    if not isinstance(relationships, dict):
        return doc
    evaluator.accessible_concept_ids(_relationship_concept_ids(relationships))
    new_relationships: Dict[str, Any] = {}
    changed = False
    for predicate, value in relationships.items():
        sanitised, predicate_changed = _sanitize_relationship_value(value, evaluator)
        if sanitised is _REMOVE:
            changed = True
            continue
        if predicate_changed:
            changed = True
        new_relationships[predicate] = sanitised
    if changed:
        doc = dict(doc)
        doc["relationships"] = new_relationships
    return doc


def can_access_concept(concept_id: Any) -> bool:
    evaluator = _current_evaluator()
    return evaluator.can_access(concept_id)


def filter_accessible_concept_ids(concept_ids: Iterable[Any]) -> set[str]:
    evaluator = _current_evaluator()
    return evaluator.accessible_concept_ids(concept_ids)


def invalidate_current_access_evaluator() -> None:
    """Discard actor-visibility decisions cached in the current context.

    Most concept reads benefit from one request-local evaluator.  Derived
    authority surfaces that intentionally outlive a single read (for example a
    same-turn workflow-discovery memo) must be able to re-check live Vontology
    visibility rather than inheriting an earlier positive decision.  Clearing
    both storage locations keeps this safe in Flask requests and background
    workflow contexts.
    """

    _EVALUATOR.set(None)
    if has_request_context():
        g._von_access_evaluator = None


def cache_scope_key() -> str:
    if not should_enforce_access_control():
        return "global"
    user_id = get_effective_user_concept_id()
    org_id = get_effective_organisation_concept_id()
    return f"user:{user_id or 'anon'}|org:{org_id or 'none'}"


@contextmanager
def override_current_user(concept_id: Optional[str]):
    token = _MANUAL_USER.set(_normalise_concept_id(concept_id))
    eval_token = _EVALUATOR.set(None)
    try:
        yield
    finally:
        _EVALUATOR.reset(eval_token)
        _MANUAL_USER.reset(token)


@contextmanager
def override_current_organisation(concept_id: Optional[str]):
    token = _MANUAL_ORG.set(_normalise_stored_org_concept_id(concept_id))
    eval_token = _EVALUATOR.set(None)
    try:
        yield
    finally:
        _EVALUATOR.reset(eval_token)
        _MANUAL_ORG.reset(token)


@contextmanager
def override_current_actor(
    user_concept_id: Optional[str] = None,
    organisation_concept_id: Optional[str] = None,
):
    """Apply one explicit actor scope to access-controlled support lookups."""

    bound_token = _MANUAL_ACTOR_BOUND.set(True)
    user_token = _MANUAL_USER.set(_normalise_concept_id(user_concept_id))
    org_token = _MANUAL_ORG.set(
        _normalise_stored_org_concept_id(organisation_concept_id)
    )
    eval_token = _EVALUATOR.set(None)
    try:
        yield
    finally:
        _EVALUATOR.reset(eval_token)
        _MANUAL_ORG.reset(org_token)
        _MANUAL_USER.reset(user_token)
        _MANUAL_ACTOR_BOUND.reset(bound_token)


@contextmanager
def force_access_control_enforcement():
    """Enforce global-only visibility even when no actor identity is bound."""

    token = _FORCE_ENFORCEMENT.set(True)
    eval_token = _EVALUATOR.set(None)
    try:
        yield
    finally:
        _EVALUATOR.reset(eval_token)
        _FORCE_ENFORCEMENT.reset(token)


@contextmanager
def bypass_access_control():
    token = _BYPASS.set(True)
    eval_token = _EVALUATOR.set(None)
    try:
        yield
    finally:
        _EVALUATOR.reset(eval_token)
        _BYPASS.reset(token)
