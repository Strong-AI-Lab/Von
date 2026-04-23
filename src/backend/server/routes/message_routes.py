"""REST API routes for inter-user messaging.

JVNAUTOSCI-1071
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from flask import Blueprint, jsonify, request, session
from flask.typing import ResponseReturnValue

from ...services.message_service import (
    create_message,
    get_message,
    get_messages_for_user,
    get_conversation_between_users,
    get_unread_count,
    mark_message_read,
    mark_messages_read_bulk,
    delete_message,
    get_message_threads_for_user,
    search_messages,
)
from ...services.paper_recommendation_review_service import (
    build_message_linked_paper_recommendation_review,
)
from ...services.paper_recommendation_vontology_service import (
    record_paper_recommendation_feedback,
)

_log = logging.getLogger(__name__)

message_bp = Blueprint("messages", __name__)


def _normalise_concept_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned}"


def _normalise_recipient_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        concept_id = _normalise_concept_id(item)
        if not isinstance(concept_id, str):
            continue
        lowered = concept_id.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(concept_id)
    return result


def _normalise_concept_id_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        concept_id = _normalise_concept_id(item)
        if not isinstance(concept_id, str):
            continue
        fingerprint = concept_id.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(concept_id)
    return result


def _get_current_user_concept_id() -> Optional[str]:
    """Get the current user's concept_id from session or access control."""
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")
    if user_concept_id:
        return user_concept_id

    # Fallback to constructing from user_id
    user_id = session.get("user_id")
    if user_id:
        return f"#V#{user_id}"

    return None


def _get_current_org_concept_id(user_concept_id: Optional[str]) -> Optional[str]:
    """Get the current organisation's concept ID from effective window/session context."""
    if isinstance(user_concept_id, str) and user_concept_id.strip():
        try:
            from ...services.window_session_context_service import get_effective_context

            window_session_id = request.headers.get("X-Von-Window-Session")
            effective = get_effective_context(
                window_session_id,
                dict(session),
                user_concept_id.strip(),
            )
            org_id = _normalise_concept_id(effective.get("organisation_id"))
            if org_id:
                return org_id
        except Exception:
            pass

    return _normalise_concept_id(session.get("organisation_concept_id"))


def _get_message_metadata(message_doc: Any) -> dict[str, Any]:
    concept_data = message_doc.get("concept_data") if isinstance(message_doc, dict) else {}
    metadata = concept_data.get("metadata") if isinstance(concept_data, dict) else {}
    return dict(metadata) if isinstance(metadata, dict) else {}


def _resolve_recommendation_subject_id(
    *,
    message_doc: dict[str, Any],
    current_user_concept_id: str | None,
) -> Optional[str]:
    metadata = _get_message_metadata(message_doc)
    subject_id = _normalise_concept_id(metadata.get("recommendation_subject_concept_id"))
    if subject_id:
        return subject_id
    if current_user_concept_id:
        return _normalise_concept_id(current_user_concept_id)

    relationships = message_doc.get("relationships") if isinstance(message_doc, dict) else {}
    recipient_ids = _normalise_concept_id_list(
        relationships.get("#V#has_recipient") if isinstance(relationships, dict) else []
    )
    return recipient_ids[0] if recipient_ids else None


def _resolve_message_recommendation_assertion_ids(message_doc: dict[str, Any]) -> list[str]:
    metadata = _get_message_metadata(message_doc)
    return _normalise_concept_id_list(metadata.get("recommendation_assertion_ids"))


def _is_paper_recommendation_message(message_doc: dict[str, Any]) -> bool:
    metadata = _get_message_metadata(message_doc)
    return (
        str(metadata.get("delivery_channel") or "").strip()
        == "paper_recommendation_message"
    )


def _authorise_sender_and_recipients_for_org(
    *,
    sender_id: str,
    recipient_ids: list[str],
    organisation_concept_id: str,
) -> tuple[bool, list[str]]:
    from ...services.organisation_membership_service import get_organisation_members

    members = get_organisation_members(organisation_concept_id)
    org_member_ids = {
        normalised
        for normalised in (
            _normalise_concept_id(member.get("user_concept_id"))
            for member in members.get("members", [])
            if isinstance(member, dict)
        )
        if isinstance(normalised, str)
    }

    invalid_ids: list[str] = []
    if sender_id not in org_member_ids:
        invalid_ids.append(sender_id)
    for recipient_id in recipient_ids:
        if recipient_id not in org_member_ids:
            invalid_ids.append(recipient_id)

    return (len(invalid_ids) == 0, invalid_ids)


def _prettify_concept_id(concept_id: str) -> str:
    return concept_id.replace("#V#", "").replace("_", " ").title()


def _build_organisation_option(
    organisation_concept_id: str,
    *,
    role: str | None = None,
) -> dict[str, Any]:
    name = _prettify_concept_id(organisation_concept_id)
    try:
        from ...services.concept_service import (
            enrich_concept_with_text_relations,
            get_concept_by_concept_id,
        )
        from ...vontology.utils_vontology import (
            get_concept_display_name_with_names_fallback,
        )

        concept = get_concept_by_concept_id(concept_id=organisation_concept_id)
        if isinstance(concept, dict):
            enriched = enrich_concept_with_text_relations(concept)
            display_name = get_concept_display_name_with_names_fallback(enriched)
            if isinstance(display_name, str) and display_name.strip():
                name = display_name.strip()
    except Exception:
        pass

    payload: dict[str, Any] = {
        "concept_id": organisation_concept_id,
        "name": name,
    }
    if isinstance(role, str) and role.strip():
        payload["role"] = role.strip()
    return payload


def _build_common_organisation_options(
    *,
    sender_id: str,
    recipient_ids: list[str],
    exclude_organisation_concept_id: str | None = None,
) -> list[dict[str, Any]]:
    from ...services.organisation_membership_service import get_user_memberships

    membership_maps: list[dict[str, str]] = []
    user_ids = [sender_id, *recipient_ids]

    for user_concept_id in user_ids:
        try:
            memberships = get_user_memberships(user_concept_id)
        except Exception:
            return []

        membership_map: dict[str, str] = {}
        membership_entries = (
            memberships.get("memberships", [])
            if isinstance(memberships, dict)
            else []
        )
        for membership in membership_entries:
            if not isinstance(membership, dict):
                continue
            organisation_concept_id = _normalise_concept_id(
                membership.get("organisation_concept_id")
            )
            if not organisation_concept_id:
                continue
            role = membership.get("role")
            membership_map[organisation_concept_id] = (
                role.strip()
                if isinstance(role, str) and role.strip()
                else "member"
            )
        membership_maps.append(membership_map)

    if not membership_maps:
        return []

    common_organisation_ids = set(membership_maps[0].keys())
    for membership_map in membership_maps[1:]:
        common_organisation_ids &= set(membership_map.keys())

    excluded_id = _normalise_concept_id(exclude_organisation_concept_id)
    if excluded_id:
        common_organisation_ids.discard(excluded_id)

    sender_roles = membership_maps[0]
    options = [
        _build_organisation_option(
            organisation_concept_id,
            role=sender_roles.get(organisation_concept_id),
        )
        for organisation_concept_id in common_organisation_ids
    ]
    options.sort(
        key=lambda item: (
            str(item.get("name") or "").casefold(),
            str(item.get("concept_id") or "").casefold(),
        )
    )
    return options


@message_bp.route("/", methods=["POST"])
def send_message() -> ResponseReturnValue:
    """Send a new message.

    Request body:
    {
        "recipient_ids": ["#V#user1", "#V#user2"],
        "content": "Message text",
        "subject": "Optional subject",
        "thread_id": "Optional thread concept ID",
        "reply_to_id": "Optional message ID being replied to",
        "organisation_concept_id": "Optional explicit send scope",
        "metadata": {}
    }
    """
    sender_id = _get_current_user_concept_id()
    if not sender_id:
        return jsonify({"error": "Authentication required"}), 401

    sender_id = sender_id.strip()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body required"}), 400

    explicit_org_id = _normalise_concept_id(
        data.get("organisation_concept_id") or data.get("org_id")
    )
    org_id = explicit_org_id or _get_current_org_concept_id(sender_id)
    if not isinstance(org_id, str) or not org_id:
        return jsonify({"error": "No organisation context"}), 400

    recipient_ids = _normalise_recipient_ids(data.get("recipient_ids"))
    content = data.get("content", "").strip()

    if not recipient_ids:
        return jsonify({"error": "At least one recipient is required"}), 400
    if not content:
        return jsonify({"error": "Message content is required"}), 400

    try:
        is_authorised, invalid_ids = _authorise_sender_and_recipients_for_org(
            sender_id=sender_id,
            recipient_ids=recipient_ids,
            organisation_concept_id=org_id,
        )
        if not is_authorised:
            authorisation_error = (
                "Sender/recipient must share the selected organisation"
                if explicit_org_id
                else "Sender/recipient must share the current organisation"
            )
            return (
                jsonify(
                    {
                        "error": authorisation_error,
                        "invalid_concept_ids": invalid_ids,
                        "organisation_concept_id": org_id,
                        "common_organisation_options": _build_common_organisation_options(
                            sender_id=sender_id,
                            recipient_ids=recipient_ids,
                            exclude_organisation_concept_id=org_id,
                        ),
                    }
                ),
                403,
            )

        metadata = data.get("metadata")
        metadata_payload = metadata if isinstance(metadata, dict) else {}
        metadata_payload = dict(metadata_payload)
        metadata_payload.setdefault("delivery_channel", "interuser_message")
        metadata_payload.setdefault("intent", "info")
        metadata_payload.setdefault(
            "attribution",
            f"Sent by Von on behalf of {sender_id}",
        )

        message = create_message(
            sender_id=sender_id,
            recipient_ids=recipient_ids,
            content=content,
            subject=data.get("subject"),
            thread_id=data.get("thread_id"),
            reply_to_id=data.get("reply_to_id"),
            org_id=org_id,
            metadata=metadata_payload,
        )

        from ...services.episode_logging_service import log_episode

        log_episode(
            episode_type="interuser_message_sent",
            actor_user_id=sender_id,
            organisation_concept_id=org_id,
            payload={
                "message_id": message.get("concept_id"),
                "recipient_ids": recipient_ids,
                "intent": metadata_payload.get("intent"),
                "thread_id": data.get("thread_id"),
                "reply_to_id": data.get("reply_to_id"),
            },
            status="sent",
        )

        # Return sanitised response
        return (
            jsonify(
                {
                    "success": True,
                    "message_id": message.get("concept_id"),
                    "sent_at": message.get("concept_data", {}).get("sent_at"),
                    "attribution": metadata_payload.get("attribution"),
                }
            ),
            201,
        )

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _log.error(f"Failed to send message: {e}")
        return jsonify({"error": "Failed to send message"}), 500


@message_bp.route("/<message_id>", methods=["GET"])
def get_single_message(message_id: str) -> ResponseReturnValue:
    """Get a single message by ID."""
    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    message = get_message(message_id)
    if not message:
        return jsonify({"error": "Message not found"}), 404

    # Convert MongoDB doc to JSON-serializable format
    message["_id"] = str(message.get("_id", ""))

    return jsonify(message), 200


@message_bp.route("/<message_id>/paper_recommendation_review", methods=["GET"])
def get_message_paper_recommendation_review(message_id: str) -> ResponseReturnValue:
    """Inspect already delivered paper recommendations linked to one message."""

    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    message = get_message(message_id)
    if not message:
        return jsonify({"error": "Message not found"}), 404
    if not _is_paper_recommendation_message(message):
        return jsonify({"error": "Message is not a paper recommendation message"}), 400

    assertion_ids = _resolve_message_recommendation_assertion_ids(message)
    if not assertion_ids:
        return jsonify({"error": "Message has no recommendation assertions"}), 400

    subject_id = _resolve_recommendation_subject_id(
        message_doc=message,
        current_user_concept_id=user_id,
    )
    if not subject_id:
        return jsonify({"error": "Recommendation subject is unavailable"}), 400

    try:
        payload = build_message_linked_paper_recommendation_review(
            subject_concept_id=subject_id,
            assertion_concept_ids=assertion_ids,
            message_concept_id=message_id,
            trigger_source="message_opened",
        )
        return jsonify(payload), 200
    except Exception as e:
        _log.error(
            "Failed to build message-linked paper recommendation review for %s: %s",
            message_id,
            e,
        )
        return jsonify({"error": "Failed to build recommendation review"}), 500


@message_bp.route("/<message_id>/paper_recommendation_feedback", methods=["POST"])
def post_message_paper_recommendation_feedback(message_id: str) -> ResponseReturnValue:
    """Record feedback about one delivered paper recommendation from a message."""

    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    message = get_message(message_id)
    if not message:
        return jsonify({"error": "Message not found"}), 404
    if not _is_paper_recommendation_message(message):
        return jsonify({"error": "Message is not a paper recommendation message"}), 400

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object body required"}), 400

    message_assertion_ids = _resolve_message_recommendation_assertion_ids(message)
    explicit_assertion_id = _normalise_concept_id(data.get("assertion_concept_id"))
    assertion_id = explicit_assertion_id
    if not assertion_id and len(message_assertion_ids) == 1:
        assertion_id = message_assertion_ids[0]
    if not assertion_id:
        return jsonify({"error": "assertion_concept_id is required"}), 400
    if assertion_id not in message_assertion_ids:
        return jsonify({"error": "assertion_concept_id is not linked to this message"}), 400

    subject_id = _resolve_recommendation_subject_id(
        message_doc=message,
        current_user_concept_id=user_id,
    )
    if not subject_id:
        return jsonify({"error": "Recommendation subject is unavailable"}), 400

    try:
        payload = record_paper_recommendation_feedback(
            actor_user_concept_id=user_id,
            subject_concept_id=subject_id,
            assertion_concept_id=assertion_id,
            paper_concept_id=_normalise_concept_id(data.get("paper_concept_id")),
            recommendation_usefulness=data.get("recommendation_usefulness"),
            explanation_usefulness=data.get("explanation_usefulness"),
            feedback_text=data.get("feedback_text"),
            capture_surface="message_panel.paper_recommendation",
            organisation_concept_id=_get_current_org_concept_id(user_id),
        )
        return jsonify(payload), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _log.error(
            "Failed to record paper recommendation feedback for %s: %s",
            message_id,
            e,
        )
        return jsonify({"error": "Failed to record recommendation feedback"}), 500


@message_bp.route("/", methods=["GET"])
def list_messages() -> ResponseReturnValue:
    """List messages for the current user.

    Query parameters:
    - include_sent: "true"/"false" (default: true)
    - include_received: "true"/"false" (default: true)
    - thread_id: Filter to specific thread
    - limit: Max messages (default: 50)
    - skip: Pagination offset (default: 0)
    """
    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    include_sent = request.args.get("include_sent", "true").lower() == "true"
    include_received = request.args.get("include_received", "true").lower() == "true"
    thread_id = request.args.get("thread_id")

    try:
        limit = int(request.args.get("limit", 50))
        skip = int(request.args.get("skip", 0))
    except ValueError:
        return jsonify({"error": "Invalid limit or skip parameter"}), 400

    limit = min(limit, 100)  # Cap at 100

    messages = get_messages_for_user(
        user_id=user_id,
        include_sent=include_sent,
        include_received=include_received,
        thread_id=thread_id,
        limit=limit,
        skip=skip,
    )

    # Convert MongoDB docs
    for msg in messages:
        msg["_id"] = str(msg.get("_id", ""))

    return (
        jsonify(
            {
                "messages": messages,
                "count": len(messages),
            }
        ),
        200,
    )


@message_bp.route("/conversation/<other_user_id>", methods=["GET"])
def get_conversation(other_user_id: str) -> ResponseReturnValue:
    """Get conversation with another user.

    Query parameters:
    - limit: Max messages (default: 50)
    - skip: Pagination offset (default: 0)
    """
    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    try:
        limit = int(request.args.get("limit", 50))
        skip = int(request.args.get("skip", 0))
    except ValueError:
        return jsonify({"error": "Invalid limit or skip parameter"}), 400

    limit = min(limit, 100)

    messages = get_conversation_between_users(
        user_id_1=user_id,
        user_id_2=other_user_id,
        limit=limit,
        skip=skip,
    )

    for msg in messages:
        msg["_id"] = str(msg.get("_id", ""))

    return (
        jsonify(
            {
                "messages": messages,
                "count": len(messages),
            }
        ),
        200,
    )


@message_bp.route("/unread/count", methods=["GET"])
def get_unread_message_count() -> ResponseReturnValue:
    """Get count of unread messages for the current user."""
    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    count = get_unread_count(user_id)

    return jsonify({"unread_count": count}), 200


@message_bp.route("/<message_id>/read", methods=["POST"])
def mark_read(message_id: str) -> ResponseReturnValue:
    """Mark a single message as read."""
    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    success = mark_message_read(message_id, user_id)

    if success:
        return jsonify({"success": True}), 200
    else:
        return (
            jsonify({"success": False, "error": "Message not found or already read"}),
            404,
        )


@message_bp.route("/read/bulk", methods=["POST"])
def mark_read_bulk() -> ResponseReturnValue:
    """Mark multiple messages as read.

    Request body:
    {
        "message_ids": ["#V#message_1", "#V#message_2"]
    }
    """
    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    data = request.get_json()

    if not data or not data.get("message_ids"):
        return jsonify({"error": "message_ids array required"}), 400

    count = mark_messages_read_bulk(data["message_ids"], user_id)

    return (
        jsonify(
            {
                "success": True,
                "updated_count": count,
            }
        ),
        200,
    )


@message_bp.route("/<message_id>", methods=["DELETE"])
def delete_single_message(message_id: str) -> ResponseReturnValue:
    """Delete a message (soft delete, sender only)."""
    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    success = delete_message(message_id, user_id)

    if success:
        return jsonify({"success": True}), 200
    else:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Message not found or you don't have permission to delete it",
                }
            ),
            404,
        )


@message_bp.route("/threads", methods=["GET"])
def list_threads() -> ResponseReturnValue:
    """Get conversation threads for the current user.

    Query parameters:
    - limit: Max threads (default: 20)
    """
    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        return jsonify({"error": "Invalid limit parameter"}), 400

    limit = min(limit, 50)

    threads = get_message_threads_for_user(user_id, limit=limit)

    # Convert MongoDB docs
    for thread in threads:
        if thread.get("last_message"):
            thread["last_message"]["_id"] = str(thread["last_message"].get("_id", ""))

    return (
        jsonify(
            {
                "threads": threads,
                "count": len(threads),
            }
        ),
        200,
    )


@message_bp.route("/search", methods=["GET"])
def search() -> ResponseReturnValue:
    """Search messages.

    Query parameters:
    - q: Search query text
    - limit: Max results (default: 20)
    """
    user_id = _get_current_user_concept_id()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    query_text = request.args.get("q", "").strip()

    if not query_text:
        return jsonify({"error": "Search query 'q' is required"}), 400

    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        return jsonify({"error": "Invalid limit parameter"}), 400

    limit = min(limit, 50)

    messages = search_messages(user_id, query_text, limit=limit)

    for msg in messages:
        msg["_id"] = str(msg.get("_id", ""))

    return (
        jsonify(
            {
                "messages": messages,
                "count": len(messages),
            }
        ),
        200,
    )
