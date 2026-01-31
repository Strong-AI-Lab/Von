"""REST API routes for inter-user messaging.

JVNAUTOSCI-1071
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

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

_log = logging.getLogger(__name__)

message_bp = Blueprint("messages", __name__)


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


def _get_current_org_concept_id() -> Optional[str]:
    """Get the current organisation's concept ID from the session."""
    return session.get("organisation_concept_id")


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
        "metadata": {}
    }
    """
    sender_id = _get_current_user_concept_id()
    if not sender_id:
        return jsonify({"error": "Authentication required"}), 401

    org_id = _get_current_org_concept_id()

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    recipient_ids = data.get("recipient_ids", [])
    content = data.get("content", "").strip()

    if not recipient_ids:
        return jsonify({"error": "At least one recipient is required"}), 400
    if not content:
        return jsonify({"error": "Message content is required"}), 400

    try:
        message = create_message(
            sender_id=sender_id,
            recipient_ids=recipient_ids,
            content=content,
            subject=data.get("subject"),
            thread_id=data.get("thread_id"),
            reply_to_id=data.get("reply_to_id"),
            org_id=org_id,
            metadata=data.get("metadata"),
        )

        # Return sanitised response
        return (
            jsonify(
                {
                    "success": True,
                    "message_id": message.get("concept_id"),
                    "sent_at": message.get("concept_data", {}).get("sent_at"),
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
