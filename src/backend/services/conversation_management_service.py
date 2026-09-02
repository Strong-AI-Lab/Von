"""Actor-scoped conversation discovery and operational preferences.

Conversation transcripts and owner-visible names remain in ``chat_history``.
Hide, pin, and participant-specific display-name state are operational user
preferences, so they live in ``application_settings`` rather than Vontology.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import PyMongoError

from ..db.mongo_setup import get_application_settings_collection
from . import chat_history_service
from .opaque_cursor_service import (
    OpaqueCursorError,
    decode_opaque_cursor,
    encode_opaque_cursor,
)
from .organisation_membership_service import get_user_memberships
from .shared_conversation_service import (
    list_accepted_invites_for_user_page,
    list_accepted_invites_for_user_sessions,
    resolve_conversation_owner,
)

PREFERENCE_SCHEMA_VERSION = "conversation_preference.v1"
_PREFERENCE_SETTING_PREFIX = "conversation_preference.v1"
_SESSION_NAME_MAX_LEN = 80
_PREFERENCE_SEARCH_INDEX_NAME = "conversation_preference_actor_title_text_v1"
_PREFERENCE_INDEX_READY = False
_PREFERENCE_INDEX_LOCK = threading.Lock()
_CONVERSATION_CURSOR_TTL_SECONDS = 24 * 60 * 60
_RENAME_EVIDENCE_TTL_SECONDS = 60 * 60
TITLE_EVIDENCE_SCHEMA_VERSION = "conversation_title_evidence.v1"


class ConversationManagementError(RuntimeError):
    """Raised when conversation discovery or preference persistence fails."""


def _title_evidence_fingerprint(
    *,
    actor_user_id: str,
    owner_user_id: str,
    session_id: str,
    namespace: str | None,
    current_display_name: str | None,
    source_evidence: Mapping[str, Any],
) -> str:
    first = source_evidence.get("first_user_message")
    latest = source_evidence.get("latest_user_message")
    material = {
        "actor_user_id": actor_user_id,
        "owner_user_id": owner_user_id,
        "session_id": session_id,
        "namespace": namespace,
        "current_display_name": current_display_name,
        "updated_at": source_evidence.get("updated_at"),
        "message_count": source_evidence.get("message_count"),
        "user_message_count": source_evidence.get("user_message_count"),
        "first_user_sha256": (
            first.get("content_sha256") if isinstance(first, Mapping) else None
        ),
        "latest_user_sha256": (
            latest.get("content_sha256") if isinstance(latest, Mapping) else None
        ),
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_conversation_title_evidence(
    *,
    actor_user_id: str,
    owner_user_id: str,
    session_id: str,
    namespace: str | None,
    access_mode: str,
    current_display_name: str | None,
    source_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind bounded title evidence to one actor-visible unnamed conversation."""

    actor = _required_text(actor_user_id, field="actor_user_id")
    owner = _required_text(owner_user_id, field="owner_user_id")
    session = _required_text(session_id, field="session_id")
    display_name = (
        current_display_name.strip()
        if isinstance(current_display_name, str) and current_display_name.strip()
        else None
    )
    evidence_sufficient = source_evidence.get("evidence_sufficient") is True
    eligible = evidence_sufficient and display_name is None
    fingerprint = _title_evidence_fingerprint(
        actor_user_id=actor,
        owner_user_id=owner,
        session_id=session,
        namespace=namespace,
        current_display_name=display_name,
        source_evidence=source_evidence,
    )
    source_fingerprint = _title_evidence_fingerprint(
        actor_user_id=actor,
        owner_user_id=owner,
        session_id=session,
        namespace=namespace,
        current_display_name=None,
        source_evidence=source_evidence,
    )
    evidence_token = None
    if eligible:
        evidence_token = encode_opaque_cursor(
            purpose="conversation_rename_evidence",
            payload={
                "schema_version": TITLE_EVIDENCE_SCHEMA_VERSION,
                "actor_user_id": actor,
                "owner_user_id": owner,
                "session_id": session,
                "namespace": namespace,
                "access_mode": access_mode,
                "expected_display_name": None,
                "source_updated_at": source_evidence.get("updated_at"),
                "evidence_fingerprint": fingerprint,
                "source_evidence_fingerprint": source_fingerprint,
            },
            ttl_seconds=_RENAME_EVIDENCE_TTL_SECONDS,
        )
    return {
        "schema_version": TITLE_EVIDENCE_SCHEMA_VERSION,
        "session_id": session,
        "access_mode": access_mode,
        "current_display_name": display_name,
        "message_count": source_evidence.get("message_count"),
        "user_message_count": source_evidence.get("user_message_count"),
        "first_user_message": source_evidence.get("first_user_message"),
        "latest_user_message": source_evidence.get("latest_user_message"),
        "evidence_sufficient": evidence_sufficient,
        "eligible_for_rename": eligible,
        "ineligibility_reason": (
            None
            if eligible
            else (
                "existing_name_present"
                if display_name is not None
                else "user_message_evidence_unavailable"
            )
        ),
        "source_revision": {
            "updated_at": source_evidence.get("updated_at"),
            "evidence_fingerprint": fingerprint,
            "source_evidence_fingerprint": source_fingerprint,
        },
        "evidence_token": evidence_token,
    }


def validate_conversation_rename_evidence_token(
    *,
    evidence_token: str,
    actor_user_id: str,
    owner_user_id: str,
    session_id: str,
    namespace: str | None,
) -> dict[str, Any]:
    """Validate one signed, expiring rename-evidence binding."""

    try:
        payload = decode_opaque_cursor(
            cursor=evidence_token, purpose="conversation_rename_evidence"
        )
    except OpaqueCursorError as exc:
        raise ConversationManagementError(
            f"Invalid conversation rename evidence: {exc}"
        ) from exc
    expected = {
        "schema_version": TITLE_EVIDENCE_SCHEMA_VERSION,
        "actor_user_id": actor_user_id,
        "owner_user_id": owner_user_id,
        "session_id": session_id,
        "namespace": namespace,
        "access_mode": (
            "owner" if actor_user_id == owner_user_id else "invitee"
        ),
        "expected_display_name": None,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ConversationManagementError(
                f"Conversation rename evidence does not match {key}."
            )
    fingerprint = payload.get("evidence_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ConversationManagementError(
            "Conversation rename evidence fingerprint is missing."
        )
    source_fingerprint = payload.get("source_evidence_fingerprint")
    if not isinstance(source_fingerprint, str) or not source_fingerprint:
        raise ConversationManagementError(
            "Conversation rename source-evidence fingerprint is missing."
        )
    return payload


def _ensure_preference_indexes(collection: Any) -> None:
    global _PREFERENCE_INDEX_READY
    if _PREFERENCE_INDEX_READY:
        return
    with _PREFERENCE_INDEX_LOCK:
        if _PREFERENCE_INDEX_READY:
            return
        try:
            existing = {row.get("name") for row in collection.list_indexes()}
            if _PREFERENCE_SEARCH_INDEX_NAME not in existing:
                collection.create_index(
                    [
                        ("actor_user_id", 1),
                        ("preference_kind", 1),
                        ("search_session_name_override", "text"),
                    ],
                    name=_PREFERENCE_SEARCH_INDEX_NAME,
                    default_language="none",
                )
        except (PyMongoError, AttributeError, TypeError):
            # Existing preference behaviour must remain available if optional
            # index management is unavailable in a constrained runtime.
            pass
        _PREFERENCE_INDEX_READY = True


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
    _ensure_preference_indexes(collection)
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
    _ensure_preference_indexes(collection)
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
                        "search_session_name_override": desired[
                            "session_name_override"
                        ],
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


def search_conversation_preference_session_ids(
    *, actor_user_id: str, query: str, limit: int = 200
) -> list[str]:
    """Search actor-specific display-name overrides through their text index."""

    actor = _required_text(actor_user_id, field="actor_user_id")
    query_text = _required_text(query, field="query")
    collection = get_application_settings_collection()
    if collection is None:
        raise ConversationManagementError(
            "Could not connect to conversation preference storage."
        )
    _ensure_preference_indexes(collection)
    try:
        rows = collection.find(
            {
                "actor_user_id": actor,
                "preference_kind": PREFERENCE_SCHEMA_VERSION,
                "$text": {"$search": query_text},
            },
            {"_id": 0, "session_id": 1},
        ).limit(max(1, min(int(limit), 500)))
        return [
            str(row.get("session_id"))
            for row in rows
            if isinstance(row, Mapping)
            and isinstance(row.get("session_id"), str)
            and row.get("session_id")
        ]
    except PyMongoError as exc:
        raise ConversationManagementError(
            f"Could not search conversation display-name preferences: {exc}"
        ) from exc


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


def build_canonical_conversation_reference(
    *,
    owner_user_id: str,
    session_id: str,
    namespace: Any = None,
    organisation_concept_id: Any = None,
) -> dict[str, Any]:
    """Build the one canonical reference shape for an explicit conversation."""

    owner = _required_text(owner_user_id, field="owner_user_id")
    session = _required_text(session_id, field="session_id")
    return {
        "schema_version": "conversation_reference.v1",
        "binding_kind": "explicit_session_id",
        "session_id": session,
        "user_concept_id": owner,
        "namespace": namespace,
        "organisation_concept_id": _normalise_concept_id(organisation_concept_id),
        "include_legacy": True,
    }


def _project_actor_conversation_contract(
    *, actor_user_id: str, row: Mapping[str, Any]
) -> dict[str, Any]:
    projected = dict(row)
    session_id = str(projected.get("session_id") or "")
    shared = projected.get("shared_with_me") is True
    owner_user_id = (
        str(projected.get("shared_owner_user_id") or "") if shared else actor_user_id
    )
    access_mode = "invitee" if shared else "owner"
    trashed = projected.get("trashed") is True
    hidden = projected.get("hidden") is True
    pinned = projected.get("pinned") is True
    available_actions = [
        "conversation_get",
        "rename",
        "unhide" if hidden else "hide",
        "unpin" if pinned else "pin",
    ]
    if not shared:
        available_actions.append("restore" if trashed else "trash")
    canonical_reference = build_canonical_conversation_reference(
        owner_user_id=owner_user_id,
        session_id=session_id,
        namespace=projected.get("namespace"),
        organisation_concept_id=projected.get("organisation_concept_id"),
    )
    projected.update(
        {
            "display_name": projected.get("session_name"),
            "display_date": projected.get("last_message_at")
            or projected.get("created_at"),
            "access_mode": access_mode,
            "available_actions": available_actions,
            "lifecycle": {
                "hidden": hidden,
                "pinned": pinned,
                "trashed": trashed,
                "trashed_at": projected.get("trashed_at"),
            },
            "conversation_reference": canonical_reference,
            "conversation_ref": {
                "kind": "von_conversation_ref",
                "schema_version": "conversation_reference.v1",
                "conversation_ref": canonical_reference,
            },
        }
    )
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
    include_trashed: bool = False,
    cursor: str | None = None,
    name_present: bool | None = None,
    cursor_context: str | None = None,
) -> dict[str, Any]:
    """Page every owned and accepted-shared conversation for one actor.

    Owned rows and accepted invites are independent source streams.  The cursor
    records which bounded stream is active and its source keyset position, so
    no fixed candidate window is rescanned or mistaken for complete coverage.
    """

    actor = _required_text(actor_user_id, field="actor_user_id")
    safe_limit = max(1, min(int(limit), 100))
    if name_present is not None and not isinstance(name_present, bool):
        raise ConversationManagementError("name_present must be true or false.")
    cursor_scope = {
        "actor_user_id": actor,
        "namespace": namespace,
        "organisation_concept_id": _normalise_concept_id(organisation_concept_id),
        "include_hidden": include_hidden,
        "include_trashed": include_trashed,
        "name_present": name_present,
        # Adapters may bind additional semantics without wrapping this already
        # signed source cursor in another high-entropy token.
        "cursor_context": cursor_context,
    }
    phase = "owned"
    position: Mapping[str, Any] | None = None
    prior_coverage: Mapping[str, Any] = {}
    if cursor:
        try:
            cursor_payload = decode_opaque_cursor(
                cursor=cursor, purpose="conversation_list"
            )
        except OpaqueCursorError as exc:
            raise ConversationManagementError(f"Invalid conversation cursor: {exc}") from exc
        if cursor_payload.get("scope") != cursor_scope:
            raise ConversationManagementError(
                "Invalid conversation cursor: actor or filters do not match."
            )
        phase = str(cursor_payload.get("phase") or "")
        if phase not in {"owned", "shared"}:
            raise ConversationManagementError(
                "Invalid conversation cursor: phase is missing."
            )
        raw_position = cursor_payload.get("position")
        if raw_position is not None and not isinstance(raw_position, Mapping):
            raise ConversationManagementError(
                "Invalid conversation cursor: position is missing."
            )
        position = raw_position
        raw_coverage = cursor_payload.get("coverage")
        if isinstance(raw_coverage, Mapping):
            prior_coverage = raw_coverage

    warnings: list[str] = []
    conversations: list[dict[str, Any]] = []
    hidden_count = 0
    trashed_count = 0
    accepted_invites_available = (
        prior_coverage.get("accepted_invites_available") is not False
    )
    membership_lookup_available = (
        prior_coverage.get("membership_lookup_available") is not False
    )
    shared_summary_page_complete = (
        prior_coverage.get("shared_summary_page_complete") is not False
    )
    next_cursor = None

    if any(
        state is False
        for state in (
            accepted_invites_available,
            membership_lookup_available,
            shared_summary_page_complete,
        )
    ):
        warnings.append("prior_page_coverage_incomplete")

    def _cursor_coverage() -> dict[str, bool]:
        return {
            "accepted_invites_available": accepted_invites_available,
            "membership_lookup_available": membership_lookup_available,
            "shared_summary_page_complete": shared_summary_page_complete,
        }

    def _append_visible(rows: Iterable[Mapping[str, Any]]) -> None:
        nonlocal hidden_count, trashed_count
        preferred = apply_conversation_preferences(
            actor_user_id=actor, conversations=rows
        )
        for row in preferred:
            projected = _project_actor_conversation_contract(
                actor_user_id=actor, row=row
            )
            if projected.get("trashed") is True:
                trashed_count += 1
                if not include_trashed:
                    continue
            if projected.get("hidden") is True:
                hidden_count += 1
                if not include_hidden:
                    continue
            if isinstance(name_present, bool):
                has_name = bool(str(projected.get("session_name") or "").strip())
                if has_name != name_present:
                    continue
            conversations.append(projected)

    if phase == "owned":
        owned_page = chat_history_service.get_chat_history_session_summaries_page(
            actor,
            namespace=namespace,
            include_legacy=True,
            page_size=safe_limit,
            position=position,
        )
        owned_rows = [
            dict(row)
            for row in owned_page.get("sessions", [])
            if isinstance(row, Mapping)
        ]
        shared_stub_ids: set[str] = set()
        try:
            accepted_for_owned = list_accepted_invites_for_user_sessions(
                user_concept_id=actor,
                session_ids=[str(row.get("session_id") or "") for row in owned_rows],
            )
            shared_stub_ids = {
                str(invite.get("session_id"))
                for invite in accepted_for_owned
                if isinstance(invite, Mapping)
                and isinstance(invite.get("session_id"), str)
            }
        except Exception as exc:  # noqa: BLE001 - coverage remains explicit
            accepted_invites_available = False
            warnings.append(f"accepted_invites_unavailable:{type(exc).__name__}")
        _append_visible(
            row
            for row in owned_rows
            if str(row.get("session_id") or "") not in shared_stub_ids
        )
        if owned_page.get("has_more") is True:
            next_cursor = encode_opaque_cursor(
                purpose="conversation_list",
                payload={
                    "scope": cursor_scope,
                    "phase": "owned",
                    "position": owned_page.get("next_position"),
                    "coverage": _cursor_coverage(),
                },
                ttl_seconds=_CONVERSATION_CURSOR_TTL_SECONDS,
            )
        else:
            phase = "shared"
            position = None

    if phase == "shared" and next_cursor is None and len(conversations) < safe_limit:
        remaining = max(1, safe_limit - len(conversations))
        try:
            invite_page = list_accepted_invites_for_user_page(
                user_concept_id=actor,
                page_size=remaining,
                position=position,
            )
        except Exception as exc:  # noqa: BLE001 - fail soft with explicit coverage
            invite_page = {
                "available": False,
                "invites": [],
                "has_more": False,
                "next_position": None,
            }
            warnings.append(f"accepted_invites_unavailable:{type(exc).__name__}")
        current_invites_available = invite_page.get("available") is True
        accepted_invites_available = (
            accepted_invites_available and current_invites_available
        )
        if not current_invites_available:
            warnings.append("accepted_invites_unavailable")

        membership_org_ids: set[str] | None
        try:
            membership_result = get_user_memberships(actor)
            membership_org_ids = {
                org_id
                for row in membership_result.get("memberships", [])
                if isinstance(row, Mapping)
                for org_id in [
                    _normalise_concept_id(row.get("organisation_concept_id"))
                ]
                if org_id is not None
            }
        except Exception as exc:  # noqa: BLE001 - shared reads fail closed
            membership_org_ids = None
            membership_lookup_available = False
            warnings.append(
                f"organisation_memberships_unavailable:{type(exc).__name__}"
            )

        requested_org = _normalise_concept_id(organisation_concept_id)
        authorised: list[tuple[Mapping[str, Any], str, str, str | None]] = []
        for invite in invite_page.get("invites", []):
            if not isinstance(invite, Mapping):
                shared_summary_page_complete = False
                continue
            invite_org = _normalise_concept_id(
                invite.get("organisation_concept_id")
            )
            if requested_org and invite_org and requested_org != invite_org:
                continue
            if invite_org and (
                membership_org_ids is None or invite_org not in membership_org_ids
            ):
                warnings.append("shared_conversation_membership_required")
                continue
            session_id = str(invite.get("session_id") or "").strip()
            if not session_id:
                shared_summary_page_complete = False
                warnings.append("shared_conversation_session_id_missing")
                continue
            owner_id = _normalise_concept_id(
                invite.get("conversation_owner_user_id")
                or invite.get("inviter_user_id")
            )
            if owner_id is None:
                owner_id = _normalise_concept_id(
                    resolve_conversation_owner(session_id=session_id)
                )
            if owner_id is None:
                shared_summary_page_complete = False
                warnings.append(
                    f"shared_conversation_owner_unresolved:{session_id}"
                )
                continue
            owner_namespace = chat_history_service.resolve_chat_history_namespace(
                owner_id
            )
            authorised.append((invite, session_id, owner_id, owner_namespace))

        shared_summaries: dict[tuple[str, str], Mapping[str, Any]] = {}
        owner_pairs = [(owner_id, session_id) for _, session_id, owner_id, _ in authorised]
        for start in range(0, len(owner_pairs), 50):
            chunk = owner_pairs[start : start + 50]
            try:
                shared_summaries.update(
                    chat_history_service.get_chat_history_session_summaries_for_owner_sessions(
                        chunk, limit=len(chunk)
                    )
                )
            except Exception as exc:  # noqa: BLE001 - typed partial page
                shared_summary_page_complete = False
                warnings.append(
                    f"shared_conversation_summary_unavailable:{type(exc).__name__}"
                )
        shared_rows: list[dict[str, Any]] = []
        for invite, session_id, owner_id, owner_namespace in authorised:
            summary = shared_summaries.get((owner_id, session_id))
            if not isinstance(summary, Mapping):
                shared_summary_page_complete = False
                warnings.append(
                    f"shared_conversation_summary_missing:{session_id}"
                )
                continue
            row = dict(summary)
            row.update(
                {
                    "namespace": row.get("namespace") or owner_namespace,
                    "organisation_concept_id": row.get("organisation_concept_id")
                    or invite.get("organisation_concept_id"),
                    "shared_with_me": True,
                    "shared_owner_user_id": owner_id,
                    "shared_from_user_id": _normalise_concept_id(
                        invite.get("inviter_user_id")
                    ),
                    "invite_id": invite.get("invite_id"),
                }
            )
            shared_rows.append(row)
        _append_visible(shared_rows)
        if invite_page.get("has_more") is True:
            next_cursor = encode_opaque_cursor(
                purpose="conversation_list",
                payload={
                    "scope": cursor_scope,
                    "phase": "shared",
                    "position": invite_page.get("next_position"),
                    "coverage": _cursor_coverage(),
                },
                ttl_seconds=_CONVERSATION_CURSOR_TTL_SECONDS,
            )

    has_more = next_cursor is not None
    coverage_complete = (
        not has_more
        and accepted_invites_available
        and membership_lookup_available
        and shared_summary_page_complete
    )
    return {
        "success": True,
        "conversations": conversations[:safe_limit],
        "count": len(conversations),
        "limit": safe_limit,
        "include_hidden": include_hidden,
        "include_trashed": include_trashed,
        "hidden_count": hidden_count,
        "trashed_count": trashed_count,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "coverage_complete": coverage_complete,
        "coverage": {
            "source_paging": "bounded_keyset",
            "stream_order": ["owned", "accepted_shared"],
            "active_stream": phase,
            "owned_window_complete": phase == "shared",
            "accepted_invites_available": accepted_invites_available,
            "membership_lookup_available": membership_lookup_available,
            "shared_summary_page_complete": shared_summary_page_complete,
            "fixed_candidate_window": None,
        },
        "ordering": {
            "streams": ["owned_updated_desc", "accepted_shared_invite_updated_desc"],
            "tie_breaker": "session_id_or_invite_id",
            "consistency": "stateless_keyset_under_unchanged_corpus",
        },
        "warnings": warnings,
    }
