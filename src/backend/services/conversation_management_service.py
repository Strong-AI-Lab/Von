"""Actor-scoped conversation discovery and operational preferences.

Conversation transcripts and owner-visible names remain in ``chat_history``.
Hide, pin, and participant-specific display-name state are operational user
preferences, so they live in ``application_settings`` rather than Vontology.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import PyMongoError

from ..db.mongo_setup import get_application_settings_collection
from . import chat_history_service
from .organisation_membership_service import get_user_memberships
from .shared_conversation_service import (
    list_accepted_invites_for_user,
    resolve_conversation_owner,
)

PREFERENCE_SCHEMA_VERSION = "conversation_preference.v1"
_PREFERENCE_SETTING_PREFIX = "conversation_preference.v1"
_SESSION_NAME_MAX_LEN = 80


class ConversationManagementError(RuntimeError):
    """Raised when conversation discovery or preference persistence fails."""


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConversationManagementError(f"{field} is required.")
    return value.strip()


def _preference_setting_name(actor_user_id: str, session_id: str) -> str:
    actor = _required_text(actor_user_id, field="actor_user_id")
    session = _required_text(session_id, field="session_id")
    digest = hashlib.sha256(f"{actor}\x00{session}".encode()).hexdigest()
    return f"{_PREFERENCE_SETTING_PREFIX}.{digest}"


def _default_preference(session_id: str) -> dict[str, Any]:
    return {
        "schema_version": PREFERENCE_SCHEMA_VERSION,
        "session_id": session_id,
        "preference_present": False,
        "hidden": False,
        "pinned": False,
        "session_name_override": None,
        "updated_at": None,
    }


def _project_preference(
    doc: Mapping[str, Any] | None, session_id: str
) -> dict[str, Any]:
    projected = _default_preference(session_id)
    if not isinstance(doc, Mapping):
        return projected
    value = doc.get("value")
    if not isinstance(value, Mapping):
        value = {}
    name_override = value.get("session_name_override")
    projected.update(
        {
            "preference_present": True,
            "hidden": value.get("hidden") is True,
            "pinned": value.get("pinned") is True,
            "session_name_override": (
                name_override.strip()
                if isinstance(name_override, str) and name_override.strip()
                else None
            ),
            "updated_at": (
                doc.get("updated_at").isoformat()
                if isinstance(doc.get("updated_at"), datetime)
                else doc.get("updated_at")
            ),
        }
    )
    return projected


def get_conversation_preferences(
    *, actor_user_id: str, session_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Return per-session preferences, defaulting absent rows to false values."""

    actor = _required_text(actor_user_id, field="actor_user_id")
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for raw_session_id in session_ids:
        if not isinstance(raw_session_id, str) or not raw_session_id.strip():
            continue
        session_id = raw_session_id.strip()
        if session_id in seen:
            continue
        seen.add(session_id)
        ordered_ids.append(session_id)
    if not ordered_ids:
        return {}

    names_by_session = {
        session_id: _preference_setting_name(actor, session_id)
        for session_id in ordered_ids
    }
    collection = get_application_settings_collection()
    if collection is None:
        raise ConversationManagementError(
            "Could not connect to conversation preference storage."
        )
    try:
        rows = collection.find(
            {
                "setting_name": {"$in": list(names_by_session.values())},
                "actor_user_id": actor,
                "preference_kind": PREFERENCE_SCHEMA_VERSION,
            },
            {
                "_id": 0,
                "setting_name": 1,
                "session_id": 1,
                "value": 1,
                "updated_at": 1,
            },
        )
        docs_by_session = {
            str(row.get("session_id")): row
            for row in rows
            if isinstance(row, Mapping)
            and isinstance(row.get("session_id"), str)
            and row.get("setting_name")
            == names_by_session.get(str(row.get("session_id")))
        }
    except PyMongoError as exc:
        raise ConversationManagementError(
            f"Could not read conversation preferences: {exc}"
        ) from exc

    return {
        session_id: _project_preference(docs_by_session.get(session_id), session_id)
        for session_id in ordered_ids
    }


def set_conversation_preference(
    *,
    actor_user_id: str,
    session_id: str,
    hidden: bool | None = None,
    pinned: bool | None = None,
    session_name_override: str | None = None,
    update_session_name_override: bool = False,
) -> dict[str, Any]:
    """Persist one actor's reversible conversation preference with read-back."""

    actor = _required_text(actor_user_id, field="actor_user_id")
    session = _required_text(session_id, field="session_id")
    if hidden is not None and not isinstance(hidden, bool):
        raise ConversationManagementError("hidden must be a boolean.")
    if pinned is not None and not isinstance(pinned, bool):
        raise ConversationManagementError("pinned must be a boolean.")
    normalised_name_override = None
    if update_session_name_override:
        if session_name_override is not None and not isinstance(
            session_name_override, str
        ):
            raise ConversationManagementError(
                "session_name_override must be a string or null."
            )
        normalised_name_override = (
            session_name_override.strip()
            if isinstance(session_name_override, str) and session_name_override.strip()
            else None
        )
        if (
            isinstance(normalised_name_override, str)
            and len(normalised_name_override) > _SESSION_NAME_MAX_LEN
        ):
            normalised_name_override = (
                normalised_name_override[: _SESSION_NAME_MAX_LEN - 3].rstrip() + "..."
            )
    if hidden is None and pinned is None and not update_session_name_override:
        raise ConversationManagementError(
            "At least one conversation preference field must be supplied."
        )

    setting_name = _preference_setting_name(actor, session)
    collection = get_application_settings_collection()
    if collection is None:
        raise ConversationManagementError(
            "Could not connect to conversation preference storage."
        )
    query = {
        "setting_name": setting_name,
        "actor_user_id": actor,
        "session_id": session,
        "preference_kind": PREFERENCE_SCHEMA_VERSION,
    }
    try:
        existing_doc = collection.find_one(
            query, {"_id": 0, "value": 1, "updated_at": 1}
        )
        before = _project_preference(existing_doc, session)
        desired = {
            "hidden": before["hidden"] if hidden is None else hidden,
            "pinned": before["pinned"] if pinned is None else pinned,
            "session_name_override": (
                before["session_name_override"]
                if not update_session_name_override
                else normalised_name_override
            ),
        }
        changed = any(before[key] != desired[key] for key in desired)
        if changed:
            now = datetime.now(UTC)
            result = collection.update_one(
                query,
                {
                    "$set": {
                        "setting_name": setting_name,
                        "actor_user_id": actor,
                        "session_id": session,
                        "preference_kind": PREFERENCE_SCHEMA_VERSION,
                        "value": desired,
                        "updated_at": now,
                    }
                },
                upsert=True,
            )
            if getattr(result, "acknowledged", True) is False:
                raise ConversationManagementError(
                    "Conversation preference update was not acknowledged."
                )
        read_back_doc = collection.find_one(
            query, {"_id": 0, "value": 1, "updated_at": 1}
        )
    except PyMongoError as exc:
        raise ConversationManagementError(
            f"Could not update conversation preference: {exc}"
        ) from exc

    read_back = _project_preference(read_back_doc, session)
    if changed and not read_back["preference_present"]:
        raise ConversationManagementError(
            "Conversation preference canonical read-back failed."
        )
    for key, value in desired.items():
        if read_back[key] != value:
            raise ConversationManagementError(
                "Conversation preference canonical read-back did not match the update."
            )
    return {"changed": changed, "preference": read_back}


def apply_conversation_preferences(
    *, actor_user_id: str, conversations: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Overlay actor-specific UI state onto conversation summary rows."""

    rows = [dict(row) for row in conversations if isinstance(row, Mapping)]
    preferences = get_conversation_preferences(
        actor_user_id=actor_user_id,
        session_ids=[str(row.get("session_id") or "") for row in rows],
    )
    projected: list[dict[str, Any]] = []
    for row in rows:
        session_id = str(row.get("session_id") or "").strip()
        preference = preferences.get(session_id, _default_preference(session_id))
        name_override = preference.get("session_name_override")
        if isinstance(name_override, str) and name_override:
            row["session_name"] = name_override
            row["session_name_source"] = "actor_preference"
        row["hidden"] = preference.get("hidden") is True
        row["pinned"] = preference.get("pinned") is True
        row["conversation_preference"] = preference
        projected.append(row)
    return projected


def _normalise_concept_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned.lstrip('#')}"


def list_actor_conversations(
    *,
    actor_user_id: str,
    namespace: str | None = None,
    organisation_concept_id: str | None = None,
    limit: int = 20,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """List recent owned and accepted-shared conversations for one actor."""

    actor = _required_text(actor_user_id, field="actor_user_id")
    safe_limit = max(1, min(int(limit), 100))
    scan_limit = min(200, max(safe_limit, safe_limit * 2))
    owned_result = chat_history_service.get_chat_history_session_summaries_result(
        actor,
        limit=scan_limit,
        namespace=namespace,
        include_legacy=True,
        summary_mode="light",
        agent_visibility=chat_history_service.CHAT_SESSION_AGENT_VISIBILITY_INCLUDE,
        keep_newest_agent_created=False,
    )
    owned_rows = [
        dict(row)
        for row in owned_result.get("sessions", [])
        if isinstance(row, Mapping)
    ]
    owned_rows_by_session = {
        str(row.get("session_id")): row
        for row in owned_rows
        if isinstance(row.get("session_id"), str)
    }
    shared_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        accepted_invites = list_accepted_invites_for_user(user_concept_id=actor)
    except Exception as exc:  # noqa: BLE001 - list discovery is deliberately fail-soft
        accepted_invites = []
        warnings.append(f"accepted_invites_unavailable:{type(exc).__name__}")

    membership_org_ids: set[str] | None
    try:
        membership_result = get_user_memberships(actor)
        membership_org_ids = {
            org_id
            for row in membership_result.get("memberships", [])
            if isinstance(row, Mapping)
            for org_id in [_normalise_concept_id(row.get("organisation_concept_id"))]
            if org_id is not None
        }
    except Exception as exc:  # noqa: BLE001 - shared reads fail closed on membership lookup
        membership_org_ids = None
        warnings.append(f"organisation_memberships_unavailable:{type(exc).__name__}")

    requested_org = _normalise_concept_id(organisation_concept_id)
    for invite in accepted_invites:
        if not isinstance(invite, Mapping):
            continue
        invite_org = _normalise_concept_id(invite.get("organisation_concept_id"))
        if requested_org and invite_org and requested_org != invite_org:
            continue
        if invite_org and (
            membership_org_ids is None or invite_org not in membership_org_ids
        ):
            warnings.append("shared_conversation_membership_required")
            continue
        session_id = invite.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            continue
        session_id = session_id.strip()
        owner_id = _normalise_concept_id(
            invite.get("conversation_owner_user_id") or invite.get("inviter_user_id")
        )
        if owner_id is None:
            owner_id = _normalise_concept_id(
                resolve_conversation_owner(session_id=session_id)
            )
        if owner_id is None:
            warnings.append(f"shared_conversation_owner_unresolved:{session_id}")
            continue
        owner_namespace = chat_history_service.resolve_chat_history_namespace(owner_id)
        try:
            summary = chat_history_service.get_chat_history_session_summary(
                owner_id,
                session_id,
                namespace=owner_namespace,
                include_legacy=True,
                summary_mode="light",
            )
            if not isinstance(summary, Mapping):
                summary = chat_history_service.get_chat_history_session_summary(
                    owner_id,
                    session_id,
                    namespace=None,
                    include_legacy=True,
                    summary_mode="light",
                )
        except Exception as exc:  # noqa: BLE001 - one bad shared row must not hide others
            warnings.append(
                f"shared_conversation_summary_unavailable:{session_id}:{type(exc).__name__}"
            )
            continue
        if not isinstance(summary, Mapping):
            summary = owned_rows_by_session.get(session_id)
        if not isinstance(summary, Mapping):
            continue
        row = dict(summary)
        row.update(
            {
                "shared_with_me": True,
                "shared_owner_user_id": owner_id,
                "shared_from_user_id": _normalise_concept_id(
                    invite.get("inviter_user_id")
                ),
                "invite_id": invite.get("invite_id"),
            }
        )
        shared_rows.append(row)

    # Accepted shared conversations can have a local join/session stub with the
    # same identifier. Prefer the owner's authoritative summary and shared
    # access metadata, rather than misclassifying that stub as actor-owned.
    shared_session_ids = {
        str(row.get("session_id"))
        for row in shared_rows
        if isinstance(row.get("session_id"), str)
    }
    owned_rows = [
        row
        for row in owned_rows
        if str(row.get("session_id") or "") not in shared_session_ids
    ]

    combined = apply_conversation_preferences(
        actor_user_id=actor, conversations=[*owned_rows, *shared_rows]
    )
    combined.sort(
        key=lambda row: str(row.get("last_message_at") or row.get("created_at") or ""),
        reverse=True,
    )
    hidden_count = sum(1 for row in combined if row.get("hidden") is True)
    if not include_hidden:
        combined = [row for row in combined if row.get("hidden") is not True]
    conversations = combined[:safe_limit]
    for row in conversations:
        owner_id = (
            row.get("shared_owner_user_id")
            if row.get("shared_with_me") is True
            else actor
        )
        row["conversation_ref"] = {
            "kind": "von_conversation_ref",
            "conversation_ref": {
                "session_id": row.get("session_id"),
                "user_concept_id": owner_id,
                "namespace": row.get("namespace"),
                "organisation_concept_id": _normalise_concept_id(
                    row.get("organisation_concept_id")
                ),
                "include_legacy": True,
            },
        }
    return {
        "success": True,
        "conversations": conversations,
        "count": len(conversations),
        "limit": safe_limit,
        "include_hidden": include_hidden,
        "hidden_count": hidden_count,
        "has_more": len(combined) > safe_limit,
        "warnings": warnings,
    }
