"""Actor-scoped, source-backed cards for materialised conversation identities.

Conversation concepts are private derived identities.  Chat history and the
accepted-invite record remain authoritative for whether a current actor may
receive a card or backlink, and for every displayed source field.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import chat_history_service
from .conversation_concept_service import (
    build_focal_conversation_backlinks,
    build_source_backed_conversation_projection,
    get_visible_materialised_conversation_ids_for_sessions,
)
from . import shared_conversation_service


class ConversationProjectionNotFoundError(LookupError):
    """The current actor does not have a visible conversation source."""


class ConversationProjectionAccessChangedError(PermissionError):
    """A shared conversation's stored focus is no longer actor-visible."""


def authorise_focal_concepts(
    raw_ids: Any,
    *,
    maximum: int = 4,
    allow_partial: bool = False,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Resolve focal concepts through the request's actor-filtered repository."""

    from ..db.repositories.concepts_repository import ConceptsRepository
    from ..vontology.utils_vontology import get_concept_display_name_with_names_fallback

    raw_values = [raw_ids] if isinstance(raw_ids, str) else raw_ids
    if raw_values is None:
        return [], [], []
    if not isinstance(raw_values, list):
        return [], [], ["invalid_focal_concept_ids"]

    requested: list[str] = []
    invalid: list[str] = []
    for value in raw_values:
        if not isinstance(value, str) or not value.strip():
            invalid.append(str(value))
            continue
        concept_id = value.strip()
        if (
            not concept_id.startswith("#V#")
            or len(concept_id) > 512
            or any(character.isspace() for character in concept_id)
        ):
            invalid.append(concept_id)
            continue
        if concept_id not in requested:
            requested.append(concept_id)
    safe_maximum = min(max(int(maximum or 4), 1), 25)
    if len(requested) > safe_maximum:
        invalid.extend(requested[safe_maximum:])
        requested = requested[:safe_maximum]
    if invalid and not allow_partial:
        return [], [], invalid
    if not requested:
        return [], [], []

    visible_docs = list(
        ConceptsRepository.find(
            {"concept_id": {"$in": requested}},
            {"concept_id": 1, "name": 1, "names": 1, "relationships": 1},
            limit=safe_maximum,
        )
    )
    by_id = {
        doc.get("concept_id"): doc
        for doc in visible_docs
        if isinstance(doc, Mapping) and isinstance(doc.get("concept_id"), str)
    }
    missing = [concept_id for concept_id in requested if concept_id not in by_id]
    if missing and not allow_partial:
        return [], [], [*invalid, *missing]
    visible_ids = [concept_id for concept_id in requested if concept_id in by_id]
    concepts: list[dict[str, Any]] = []
    for concept_id in visible_ids:
        doc = dict(by_id[concept_id])
        relationships = doc.get("relationships")
        raw_type_ids = (
            relationships.get("is_an_instance_of")
            if isinstance(relationships, Mapping)
            else None
        )
        type_ids = [raw_type_ids] if isinstance(raw_type_ids, str) else raw_type_ids
        concepts.append(
            {
                "concept_id": concept_id,
                "name": get_concept_display_name_with_names_fallback(doc)
                or doc.get("name")
                or concept_id,
                "type_ids": [
                    item for item in type_ids if isinstance(item, str)
                ]
                if isinstance(type_ids, list)
                else [],
            }
        )
    return visible_ids, concepts, [*invalid, *missing]


def _card_activity(summary: Mapping[str, Any]) -> Any:
    return (
        summary.get("last_message_at")
        or summary.get("updated_at")
        or summary.get("created_at")
    )


def _normalise_row_focal_ids(raw_ids: Any) -> list[str]:
    """Normalise stored focus without making an authority decision."""

    raw_values = [raw_ids] if isinstance(raw_ids, str) else raw_ids
    if not isinstance(raw_values, list):
        return []
    result: list[str] = []
    for value in raw_values:
        if not isinstance(value, str):
            continue
        concept_id = value.strip()
        if (
            not concept_id.startswith("#V#")
            or len(concept_id) > 512
            or any(character.isspace() for character in concept_id)
            or concept_id in result
        ):
            continue
        result.append(concept_id)
        if len(result) == 4:
            break
    return result


def _authorise_focal_concepts_batch(
    raw_focal_lists: list[Any], *, maximum: int = 200
) -> dict[str, dict[str, Any]]:
    """Resolve all catalogue focus IDs in one actor-filtered repository read."""

    from ..db.repositories.concepts_repository import ConceptsRepository
    from ..vontology.utils_vontology import get_concept_display_name_with_names_fallback

    requested: list[str] = []
    for raw_ids in raw_focal_lists:
        for concept_id in _normalise_row_focal_ids(raw_ids):
            if concept_id not in requested:
                requested.append(concept_id)
            if len(requested) >= maximum:
                break
        if len(requested) >= maximum:
            break
    if not requested:
        return {}
    documents = list(
        ConceptsRepository.find(
            {"concept_id": {"$in": requested}},
            {"concept_id": 1, "name": 1, "names": 1, "relationships": 1},
            limit=len(requested),
        )
    )
    by_id: dict[str, dict[str, Any]] = {}
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        concept_id = document.get("concept_id")
        if not isinstance(concept_id, str) or concept_id not in requested:
            continue
        relationships = document.get("relationships")
        raw_type_ids = (
            relationships.get("is_an_instance_of")
            if isinstance(relationships, Mapping)
            else None
        )
        type_ids = [raw_type_ids] if isinstance(raw_type_ids, str) else raw_type_ids
        by_id[concept_id] = {
            "concept_id": concept_id,
            "name": get_concept_display_name_with_names_fallback(dict(document))
            or document.get("name")
            or concept_id,
            "type_ids": [item for item in type_ids if isinstance(item, str)]
            if isinstance(type_ids, list)
            else [],
        }
    return by_id


def _visible_row_focus(
    row: Mapping[str, Any], visible_by_id: Mapping[str, dict[str, Any]]
) -> tuple[list[str], list[dict[str, Any]]]:
    focal_ids = _normalise_row_focal_ids(row.get("focal_concept_ids"))
    visible_ids = [concept_id for concept_id in focal_ids if concept_id in visible_by_id]
    return visible_ids, [visible_by_id[concept_id] for concept_id in visible_ids]


def _legacy_session_projection(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Keep the legacy launcher payload deliberately free of private metadata."""

    session_id = row.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    session_name = row.get("session_name")
    return {
        "session_id": session_id.strip(),
        "session_name": (
            session_name.strip()
            if isinstance(session_name, str) and session_name.strip()
            else None
        ),
        "last_activity_at": _card_activity(row),
    }


def get_conversation_projection_for_session(
    *,
    actor_user_id: str,
    session_id: str,
    actor_namespace: str | None,
    organisation_concept_id: str | None = None,
) -> dict[str, Any]:
    """Return a fresh card for an owner or accepted shared conversation."""

    if not isinstance(actor_user_id, str) or not actor_user_id.strip():
        raise ConversationProjectionNotFoundError("Session not found")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ConversationProjectionNotFoundError("Session not found")
    actor_user_id = actor_user_id.strip()
    session_id = session_id.strip()
    invite = shared_conversation_service.get_accepted_invite_for_user_session(
        user_concept_id=actor_user_id, session_id=session_id
    )
    owner_id = (
        (invite.get("conversation_owner_user_id") or invite.get("inviter_user_id"))
        if isinstance(invite, Mapping)
        else actor_user_id
    )
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ConversationProjectionNotFoundError("Session not found")
    owner_id = owner_id.strip()
    source_namespace = None if invite else actor_namespace
    if invite is None and not chat_history_service.has_chat_history_session(
        actor_user_id, session_id, namespace=source_namespace
    ):
        raise ConversationProjectionNotFoundError("Session not found")

    focus = chat_history_service.get_chat_session_focus(
        user_id=owner_id,
        session_id=session_id,
        namespace=source_namespace,
    )
    visible_ids, focal_concepts, unavailable = authorise_focal_concepts(
        focus.get("focal_concept_ids")
    )
    if invite is not None and unavailable:
        raise ConversationProjectionAccessChangedError(
            "focused_conversation_access_changed"
        )
    summary = chat_history_service.get_chat_history_session_summary(
        user_id=owner_id,
        session_id=session_id,
        namespace=source_namespace,
        summary_mode="light",
    )
    if not isinstance(summary, Mapping):
        raise ConversationProjectionNotFoundError("Session not found")
    card = build_source_backed_conversation_projection(
        session_id=session_id,
        owner_concept_id=owner_id,
        authorised_focal_concepts=focal_concepts,
        access_mode="shared" if invite else "owner",
        session_name=summary.get("session_name"),
        last_activity_at=_card_activity(summary),
        organisation_concept_id=(
            invite.get("organisation_concept_id")
            if isinstance(invite, Mapping)
            else organisation_concept_id
        ),
        namespace=source_namespace,
        materialise_owner_projection=invite is None,
    )
    return {
        "session_id": session_id,
        "focal_concept_ids": visible_ids,
        "focal_concepts": focal_concepts,
        "access_mode": "shared" if invite else "owner",
        "conversation_projection": card,
    }


def list_focal_conversation_backlinks(
    *,
    actor_user_id: str,
    focal_concept_id: str,
    actor_namespace: str | None,
    limit: int = 25,
) -> dict[str, Any]:
    """List bounded cards where a currently visible concept is focal.

    The initial focal check and one batched actor-filtered concept read are the
    only concept-visibility reads on this catalogue path.  Accepted invites
    supply exact owner/session candidates; their minimal chat metadata is read
    once as a bounded batch.
    """

    if not isinstance(actor_user_id, str) or not actor_user_id.strip():
        raise ConversationProjectionNotFoundError("focal_concept_not_authorised")
    if not isinstance(focal_concept_id, str) or not focal_concept_id.strip():
        raise ConversationProjectionNotFoundError("focal_concept_not_authorised")
    actor_user_id = actor_user_id.strip()
    focal_concept_id = focal_concept_id.strip()
    _, _, invalid = authorise_focal_concepts([focal_concept_id])
    if invalid:
        raise ConversationProjectionNotFoundError("focal_concept_not_authorised")
    safe_limit = min(max(int(limit or 25), 1), 25)
    candidate_limit = 50

    candidates: list[dict[str, Any]] = [
        {**row, "_projection_owner_id": actor_user_id, "_projection_access_mode": "owner"}
        for row in chat_history_service.list_chat_sessions_for_focal_concept(
            user_id=actor_user_id,
            focal_concept_id=focal_concept_id,
            namespace=actor_namespace,
        )[:candidate_limit]
        if isinstance(row, Mapping)
    ]

    try:
        accepted_invites = shared_conversation_service.list_accepted_invites_for_user(
            user_concept_id=actor_user_id, limit=candidate_limit
        )
    except Exception:
        # A transient sharing-store fault must not hide the actor's own cards.
        accepted_invites = []
    accepted_pairs: list[tuple[str, str]] = []
    accepted_invites_by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
    for invite in accepted_invites:
        if not isinstance(invite, Mapping):
            continue
        session_id = invite.get("session_id")
        owner_id = invite.get("conversation_owner_user_id") or invite.get(
            "inviter_user_id"
        )
        if not isinstance(session_id, str) or not isinstance(owner_id, str):
            continue
        key = (owner_id.strip(), session_id.strip())
        if not key[0] or not key[1] or key in accepted_invites_by_pair:
            continue
        accepted_pairs.append(key)
        accepted_invites_by_pair[key] = invite
        if len(accepted_pairs) == candidate_limit:
            break
    try:
        accepted_summaries = (
            chat_history_service.get_chat_history_session_summaries_for_owner_sessions(
                accepted_pairs, limit=candidate_limit
            )
        )
    except chat_history_service.ChatHistoryServiceError:
        accepted_summaries = {}
    for (owner_id, session_id), summary in accepted_summaries.items():
        if not isinstance(summary, Mapping):
            continue
        # This only narrows the invite-derived candidate set.  The batch
        # visibility map below makes the real actor authority decision.
        if focal_concept_id not in _normalise_row_focal_ids(
            summary.get("focal_concept_ids")
        ):
            continue
        candidates.append(
            {
                **summary,
                "_projection_owner_id": owner_id,
                "_projection_access_mode": "shared",
            }
        )

    visible_by_id = _authorise_focal_concepts_batch(
        [row.get("focal_concept_ids") for row in candidates]
    )
    visible_candidates: list[
        tuple[dict[str, Any], list[str], list[dict[str, Any]]]
    ] = []
    for candidate in candidates:
        visible_ids, visible_concepts = _visible_row_focus(candidate, visible_by_id)
        if focal_concept_id in visible_ids:
            visible_candidates.append((candidate, visible_ids, visible_concepts))
    visible_candidates.sort(
        key=lambda item: str(_card_activity(item[0]) or ""),
        reverse=True,
    )

    selected = visible_candidates[:safe_limit]
    materialised_by_session = get_visible_materialised_conversation_ids_for_sessions(
        [
            row.get("session_id")
            for row, _, _ in selected
            if row.get("_projection_access_mode") == "owner"
        ]
    )
    cards: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    for row, _, visible_concepts in selected:
        session_id = row.get("session_id")
        owner_id = row.get("_projection_owner_id")
        access_mode = row.get("_projection_access_mode")
        if (
            not isinstance(session_id, str)
            or not session_id.strip()
            or not isinstance(owner_id, str)
            or access_mode not in {"owner", "shared"}
        ):
            continue
        cards.append(
            build_source_backed_conversation_projection(
                session_id=session_id,
                owner_concept_id=owner_id,
                authorised_focal_concepts=visible_concepts,
                access_mode=access_mode,
                session_name=row.get("session_name"),
                last_activity_at=_card_activity(row),
                materialised_concept_id=(
                    materialised_by_session.get(session_id)
                    if access_mode == "owner"
                    else None
                ),
            )
        )
        legacy_row = _legacy_session_projection(row)
        if legacy_row is not None:
            sessions.append(legacy_row)
    return {
        "focal_concept_id": focal_concept_id,
        "sessions": sessions,
        "conversation_backlinks": build_focal_conversation_backlinks(
            source_concept_id=focal_concept_id,
            items=cards,
            more_count=max(len(visible_candidates) - safe_limit, 0),
        ),
        "access_scope": "actor",
    }

__all__ = [
    "ConversationProjectionAccessChangedError",
    "ConversationProjectionNotFoundError",
    "authorise_focal_concepts",
    "get_conversation_projection_for_session",
    "list_focal_conversation_backlinks",
]
