"""Bounded, activity-ordered catalogue over the two canonical stores.

Each source applies the same keyset boundary before limiting. Only metadata is
merged; an unconsumed candidate is fetched again on the next page, never lost
behind a browser-side first-page truncation. Cursors are bound to actor/scope.
"""

import logging
from datetime import UTC, datetime

from . import chat_history_service as chats
from . import message_catalogue_service as messages
from .conversation_management_service import apply_conversation_preferences
from .conversation_read_service import project_unread
from .opaque_cursor_service import decode_opaque_cursor, encode_opaque_cursor
from .organisation_membership_service import get_user_memberships
from .shared_conversation_service import list_accepted_invites_for_user

log = logging.getLogger(__name__)


def _chat_page(actor, namespace, organisation, limit, before):
    owned = chats.build_chat_history_query(
        user_id=actor, namespace=namespace, include_legacy=False
    )
    invites = list_accepted_invites_for_user(user_concept_id=actor)
    authorised = {}
    if invites:
        memberships = {
            m.get("organisation_concept_id")
            for m in get_user_memberships(actor).get("memberships", [])
        }
        for invite in invites:
            org = invite.get("organisation_concept_id")
            if org and (org != organisation or org not in memberships):
                continue
            owner = invite.get("conversation_owner_user_id") or invite.get(
                "inviter_user_id"
            )
            sid = invite.get("session_id")
            if owner and sid:
                authorised[(owner, sid)] = invite
    # Suppress local invitation stubs; fetch only the authorised owner's row.
    if authorised:
        owned["session_id"] = {"$nin": [sid for _, sid in authorised]}
    clauses = [
        owned,
        *({"user_id": owner, "session_id": sid} for owner, sid in authorised),
    ]
    query = {"$or": clauses, "trashed_at": None}
    collection = chats.get_chat_history_collection_service(read_only=True)
    if collection is None:
        raise RuntimeError("Conversations unavailable.")
    chats.backfill_conversation_activity_metadata(collection, query=query)
    if before:
        stamp = chats._coerce_datetime(before["timestamp"])
        query["$and"] = [
            {
                "$or": [
                    {"last_contribution_at": {"$lt": stamp}},
                    {
                        "last_contribution_at": stamp,
                        "$expr": {
                            "$gt": [
                                {"$concat": ["chat:", "$session_id"]},
                                before["key"],
                            ]
                        },
                    },
                ]
            }
        ]
    projection = chats._add_chat_session_provenance_projection(
        {
            key: 1
            for key in (
                "user_id",
                "session_id",
                "session_name",
                "created_at",
                "updated_at",
                "namespace",
                "organisation_concept_id",
                "last_contribution_at",
                "last_contribution_preview",
                "last_contribution_role",
                "last_contribution_author",
                "last_incoming_contribution_at",
                "last_incoming_timestamp",
                "last_incoming_turn_id",
                "focal_concept_ids",
                "trashed_at",
            )
        }
    )
    projection["_id"] = 0
    docs = list(
        chats._read_find(
            collection, query, projection, operation="conversation_catalogue.metadata"
        )
        .sort([("last_contribution_at", -1), ("session_id", 1)])
        .limit(limit + 1)
    )
    rows = []
    for doc in docs[:limit]:
        row = chats._build_session_summary_from_metadata(doc)
        if not row:
            continue
        row.update(
            source_kind="chat_session", catalogue_sort_key="chat:" + row["session_id"]
        )
        invite = authorised.get((doc.get("user_id"), row["session_id"]))
        if invite:
            row.update(
                shared_with_me=True,
                shared_owner_user_id=doc["user_id"],
                shared_from_user_id=invite.get("inviter_user_id"),
                invite_id=invite.get("invite_id"),
            )
        rows.append(row)
    return project_unread(
        actor, apply_conversation_preferences(actor_user_id=actor, conversations=rows)
    ), len(docs) > limit


def list_catalogue(
    actor, *, namespace, organisation=None, limit=100, cursor=None, all_contexts=False
):
    if not actor or not namespace:
        raise PermissionError("Authenticated conversation context required.")
    limit = max(1, min(100, int(limit)))
    scope = {
        "actor": actor,
        "namespace": namespace,
        "organisation": organisation,
        "all_contexts": all_contexts,
    }
    before = None
    if cursor:
        decoded = decode_opaque_cursor(cursor=cursor, purpose="conversation_catalogue")
        if decoded.get("scope") != scope:
            raise ValueError("The conversation cursor belongs to another context.")
        before = decoded["position"]
    rows, coverage, more = [], {}, False
    try:
        chat_rows, chat_more = _chat_page(actor, namespace, organisation, limit, before)
        rows.extend(chat_rows)
        more |= chat_more
        coverage["conversations"] = True
    except Exception:
        log.exception("Conversation catalogue source unavailable")
        coverage["conversations"] = False
    try:
        page = messages.list_exchanges(
            actor,
            organisation=organisation,
            limit=limit,
            before=before,
            all_contexts=all_contexts,
        )
        rows.extend(page["conversations"])
        more |= page["has_more"]
        coverage["messages"] = True
    except Exception:
        log.exception("Message catalogue source unavailable")
        coverage["messages"] = False
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    rows.sort(
        key=lambda row: (
            -(chats._coerce_datetime(row.get("last_message_at")) or epoch).timestamp(),
            row["catalogue_sort_key"],
        )
    )
    more |= len(rows) > limit
    selected = rows[:limit]
    next_cursor = None
    # A failed source cannot be skipped by advancing the common boundary. Keep
    # the healthy source usable and require refresh to recover full coverage.
    if more and selected and all(coverage.values()):
        last = selected[-1]
        next_cursor = encode_opaque_cursor(
            purpose="conversation_catalogue",
            payload={
                "scope": scope,
                "position": {
                    "timestamp": last["last_message_at"],
                    "key": last["catalogue_sort_key"],
                },
            },
            ttl_seconds=86400,
        )
    return {
        "conversations": selected,
        "has_more": more,
        "next_cursor": next_cursor,
        "coverage": coverage,
        "coverage_complete": all(coverage.values()),
    }
