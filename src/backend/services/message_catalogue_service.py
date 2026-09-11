"""Exact participant exchanges projected from canonical message concepts.

No transcript copy is maintained. Group recipients are part of the identity,
so a group message cannot be accidentally opened or replied to as a private DM.
"""

from __future__ import annotations

import hashlib
import json
import re

from ..db.mongo_client import get_concepts_collection
from ..security.access_control import (
    apply_concept_query_filter,
    get_effective_organisation_concept_id,
    override_current_actor,
)
from .message_service import (
    MESSAGE_TYPE_CONCEPT_ID,
    PREDICATE_RECIPIENT,
    PREDICATE_SENDER,
)
from .organisation_membership_service import get_user_memberships


def participant_expression():
    return {
        "$setUnion": [
            {"$ifNull": [f"$relationships.{PREDICATE_SENDER}", []]},
            {"$ifNull": [f"$relationships.{PREDICATE_RECIPIENT}", []]},
        ]
    }


def exchange_id(participants, organisation=None):
    return (
        "messages:"
        + hashlib.sha256(
            json.dumps([sorted(set(participants)), organisation]).encode()
        ).hexdigest()[:32]
    )


def _query(actor, organisation=None, *, all_contexts=False, include_legacy=False):
    query = {
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        "concept_data.deleted": {"$ne": True},
        "$or": [
            {f"relationships.{PREDICATE_SENDER}": actor},
            {f"relationships.{PREDICATE_RECIPIENT}": actor},
        ],
    }
    if all_contexts:
        orgs = {
            row.get("organisation_concept_id")
            for row in get_user_memberships(actor).get("memberships", [])
        }
        visible = []
        for org in [None, *sorted(org for org in orgs if org)]:
            with override_current_actor(actor, org):
                visible.append(apply_concept_query_filter(dict(query)))
        return {"$or": visible}
    if organisation is None or not include_legacy:
        query["concept_data.organisation_concept_id"] = organisation
    else:
        query["$and"] = [
            {
                "$or": [
                    {"concept_data.organisation_concept_id": organisation},
                    {"concept_data.organisation_concept_id": {"$exists": False}},
                ]
            }
        ]
    if organisation == get_effective_organisation_concept_id():
        return apply_concept_query_filter(query)
    memberships = (
        {
            row.get("organisation_concept_id")
            for row in get_user_memberships(actor).get("memberships", [])
        }
        if organisation
        else set()
    )
    with override_current_actor(
        actor, organisation if organisation in memberships else None
    ):
        return apply_concept_query_filter(query)


def list_exchanges(
    actor,
    *,
    organisation=None,
    limit=100,
    skip=0,
    query_text=None,
    all_contexts=False,
    before=None,
):
    if not actor:
        raise PermissionError("Authentication required.")
    collection = get_concepts_collection()
    if collection is None:
        raise RuntimeError("Messages unavailable.")
    query = _query(actor, organisation, all_contexts=all_contexts, include_legacy=True)
    if query_text:
        query["concept_data.content_fallback"] = {
            "$regex": re.escape(query_text[:200]),
            "$options": "i",
        }
    pipeline = [
        {"$match": query},
        {"$sort": {"created_at": -1, "concept_id": -1}},
        {
            "$group": {
                "_id": {
                    "participants": {
                        "$sortArray": {"input": participant_expression(), "sortBy": 1}
                    },
                    "organisation": {
                        "$ifNull": ["$concept_data.organisation_concept_id", None]
                    },
                },
                "last_message": {"$first": "$$ROOT"},
                "message_count": {"$sum": 1},
                "unread_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            actor,
                                            {
                                                "$ifNull": [
                                                    f"$relationships.{PREDICATE_RECIPIENT}",
                                                    [],
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        "$not": [
                                            {
                                                "$in": [
                                                    actor,
                                                    {
                                                        "$ifNull": [
                                                            "$concept_data.read_by",
                                                            [],
                                                        ]
                                                    },
                                                ]
                                            }
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$set": {
                "catalogue_sort_key": {
                    "$concat": [
                        "message:",
                        {
                            "$reduce": {
                                "input": "$_id.participants",
                                "initialValue": "",
                                "in": {"$concat": ["$$value", "$$this", "\u0000"]},
                            }
                        },
                        {"$ifNull": ["$_id.organisation", ""]},
                    ]
                }
            }
        },
    ]
    if before:
        from .chat_history_service import _coerce_datetime

        stamp = _coerce_datetime(before["timestamp"])
        pipeline.append(
            {
                "$match": {
                    "$or": [
                        {"last_message.created_at": {"$lt": stamp}},
                        {
                            "last_message.created_at": stamp,
                            "catalogue_sort_key": {"$gt": before["key"]},
                        },
                    ]
                }
            }
        )
    pipeline.extend(
        [
            {"$sort": {"last_message.created_at": -1, "catalogue_sort_key": 1}},
            {"$skip": max(0, skip)},
            {"$limit": min(200, max(1, limit)) + 1},
            {
                "$project": {
                    "_id": 1,
                    "catalogue_sort_key": 1,
                    "message_count": 1,
                    "unread_count": 1,
                    "last_message.concept_id": 1,
                    "last_message.created_at": 1,
                    "last_message.concept_data.content_fallback": 1,
                    "last_message.concept_data.organisation_concept_id": 1,
                    f"last_message.relationships.{PREDICATE_SENDER}": 1,
                }
            },
        ]
    )
    rows = list(collection.aggregate(pipeline))
    has_more = len(rows) > limit
    result = []
    for row in rows[:limit]:
        participants = row["_id"]["participants"]
        other = [p for p in participants if p != actor]
        last = row["last_message"]
        metadata = last.get("concept_data") or {}
        from .chat_history_service import _coerce_datetime

        stamp = _coerce_datetime(last.get("created_at"))
        result.append(
            {
                "session_id": exchange_id(
                    participants, metadata.get("organisation_concept_id")
                ),
                "source_kind": "message_exchange",
                "catalogue_sort_key": row["catalogue_sort_key"],
                "participant_ids": participants,
                "other_participant_ids": other,
                "viewer_id": actor,
                "session_name": ", ".join(
                    p.removeprefix("#V#").replace("_", " ") for p in other
                )
                or "Notes to yourself",
                "last_message_at": stamp.isoformat()
                if hasattr(stamp, "isoformat")
                else stamp,
                "last_message_id": last.get("concept_id"),
                "preview": metadata.get("content_fallback", "")[:240],
                "last_author_id": (
                    last.get("relationships", {}).get(PREDICATE_SENDER) or [None]
                )[0],
                "organisation_concept_id": metadata.get("organisation_concept_id"),
                "message_count": row["message_count"],
                "shared_unread_count": row["unread_count"],
            }
        )
    from .conversation_management_service import apply_conversation_preferences

    result = apply_conversation_preferences(actor_user_id=actor, conversations=result)
    return {
        "conversations": result,
        "has_more": has_more,
        "next_skip": skip + limit if has_more else None,
    }


def get_exchange(actor, participants, *, organisation=None, limit=50, before=None):
    if (
        not actor
        or not isinstance(participants, list)
        or actor not in participants
        or len(participants) > 50
    ):
        raise PermissionError("Conversation unavailable.")
    query = _query(actor, organisation)
    query["$expr"] = {"$setEquals": [participant_expression(), participants]}
    if before:
        from .chat_history_service import _coerce_datetime

        timestamp = _coerce_datetime(before.get("created_at"))
        if not timestamp or not isinstance(before.get("concept_id"), str):
            raise ValueError("Invalid message cursor.")
        query.setdefault("$and", []).append(
            {
                "$or": [
                    {"created_at": {"$lt": timestamp}},
                    {
                        "created_at": timestamp,
                        "concept_id": {"$lt": before["concept_id"]},
                    },
                ]
            }
        )
    collection = get_concepts_collection()
    if collection is None:
        raise RuntimeError("Messages unavailable.")
    rows = list(
        collection.find(query)
        .sort([("created_at", -1), ("concept_id", -1)])
        .limit(limit + 1)
    )
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = (
        {
            "created_at": rows[-1]["created_at"].isoformat(),
            "concept_id": rows[-1]["concept_id"],
        }
        if more and rows
        else None
    )
    return {
        "messages": list(reversed(rows)),
        "has_more": more,
        "before": cursor,
        "current_user_id": actor,
    }
