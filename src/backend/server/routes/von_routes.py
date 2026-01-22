from flask import (
    Blueprint,
    request,
    jsonify,
    render_template,
    current_app,
    session,
    send_file,
)
import os
import re
import time
import threading
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any
from src.workflows.onboarding_workflow import run_onboarding_workflow
from ...languagemodels.llm_interface import get_llm_client, get_active_model_name
from .settings_routes import get_all_settings_data
from ...integrations.internal_mcp import ToolCallParsingError
from ...services import chat_history_service
from ...services.settings_service import (
    get_internal_mcp_max_tool_invocations,
    get_internal_mcp_tool_batch_cap,
    get_show_tool_use_during_thinking,
    get_buttonify_model_enabled,
)
from ...services.prompt_template_service import PromptTemplateService
from ...workflows import (
    CHAT_NARRATION_WORKFLOW_ID,
    WorkflowExecutionTrace,
    insert_workflow_execution_trace,
)

# NOTE: Previous relative template_folder path ('../../frontend/...') was incorrect.
# From this file (src/backend/server/routes/von_routes.py) we need to traverse up THREE levels
# to reach the 'src' directory, then descend into frontend/web/von_interface/templates
# would raise TemplateNotFound for 'von_interface.html'.
_TEMPLATE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "../../../frontend/web/von_interface/templates"
    )
)
von_bp = Blueprint("von", __name__, template_folder=_TEMPLATE_DIR)


# ----------------- Tool progress (JVNAUTOSCI-942) -----------------

_TOOL_PROGRESS_TTL_SEC = 10 * 60
_TOOL_PROGRESS_LOCK = threading.Lock()
_TOOL_PROGRESS: dict[tuple[str, str], dict[str, Any]] = {}


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slugify_concept_id_for_key(concept_id: str) -> str:
    cleaned = (concept_id or "").strip()
    if cleaned.startswith("#V#"):
        cleaned = cleaned[3:]
    cleaned = cleaned.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    return cleaned or "unknown"


def _get_tool_progress_scope_key() -> str:
    """Return a stable scope key for tool-progress lookup.

    - Authenticated: scope is user concept id
    - Unauthenticated: scope is a session-scoped random token
    """

    user_concept_id = None
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if isinstance(user_concept_id, str) and user_concept_id.strip():
        return f"user:{user_concept_id.strip()}"

    if "tool_progress_scope" not in session:
        session["tool_progress_scope"] = secrets.token_urlsafe(16)

    return f"anon:{session.get('tool_progress_scope')}"


@von_bp.route("/api/files/upload", methods=["POST"])
def upload_file_to_blob_store_and_vontology():
    """Upload a user-provided file into the configured blob store and register it in Vontology.

    Security: user identity is derived server-side via get_effective_user_concept_id().

    Multipart form-data:
      - file: the uploaded file

    Returns JSON:
      - success
      - uploaded: { concept_id, type_concept_id, sha256, size_bytes, content_type, original_filename }
      - storage: { backend, key, uri, content_type, size_bytes, metadata }
    """

    from werkzeug.utils import secure_filename

    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return (
            jsonify(
                {
                    "success": False,
                    "error": "missing_user_context",
                    "message": "Missing user context: establish an authenticated session first.",
                }
            ),
            401,
        )

    # Ensure the upload is associated with a stable chat session so it becomes part
    # of the same persisted history that /von/generate uses.
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    session_id = session["session_id"]

    if "file" not in request.files:
        return jsonify({"success": False, "error": "missing_file"}), 400

    uploaded = request.files.get("file")
    if not uploaded or not getattr(uploaded, "filename", None):
        return jsonify({"success": False, "error": "empty_upload"}), 400

    original_filename = str(uploaded.filename)
    safe_filename = secure_filename(original_filename) or "uploaded_file"
    content_type = getattr(uploaded, "mimetype", None) or None

    try:
        data = uploaded.read()
    except Exception as exc:
        current_app.logger.warning(f"[files/upload] Failed to read upload: {exc}")
        return jsonify({"success": False, "error": "read_failed"}), 400

    if not isinstance(data, (bytes, bytearray)) or not data:
        return jsonify({"success": False, "error": "empty_bytes"}), 400

    data_bytes = bytes(data)

    import hashlib

    sha256 = hashlib.sha256(data_bytes).hexdigest()
    size_bytes = len(data_bytes)

    from ...services.blob_uploads import BlobUploadError, put_bytes_durable

    user_slug = _slugify_concept_id_for_key(user_concept_id)
    blob_key = f"uploads/{user_slug}/{sha256}/{safe_filename}"
    uploaded_at = _now_utc_iso()

    try:
        stored = put_bytes_durable(
            key=blob_key,
            data=data_bytes,
            content_type=content_type,
            sha256=sha256,
            size_bytes=size_bytes,
            metadata={
                "original_filename": original_filename,
                "user_concept_id": user_concept_id.strip(),
                "uploaded_at": uploaded_at,
            },
        )
    except BlobUploadError as exc:
        current_app.logger.error(
            "[files/upload] Blob store upload failed: %s",
            exc,
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "blob_store_upload_failed",
                    "message": str(exc),
                }
            ),
            502,
        )

    blob_ref = stored.ref

    # --- Ensure KR infrastructure exists ---
    type_concept_id = "#V#computer_file_copy"

    try:
        from ...db.repositories.concepts_repository import ConceptsRepository
        from ...vontology.utils_vontology import (
            THING_PRIMARY_ID,
            create_vontology_concept,
            ensure_thing_exists_and_link_orphans,
        )

        if not ConceptsRepository.find_one({"concept_id": type_concept_id}):
            # Prefer a store-of-information parent if present; otherwise fall back to Thing.
            parent_id = (
                "#V#store_of_information"
                if ConceptsRepository.find_one(
                    {"concept_id": "#V#store_of_information"}
                )
                else THING_PRIMARY_ID
            )
            if parent_id == THING_PRIMARY_ID:
                # Best-effort: ensure Thing exists.
                ensure_thing_exists_and_link_orphans()

            created = create_vontology_concept(
                parent_id=parent_id,
                new_concept_name="Computer File Copy",
                create_as_instance=False,
                description=(
                    "A computer file copy is an information-bearing artefact representing a specific stored byte sequence "
                    "(for example an uploaded file stored in Von's blob store)."
                ),
                notes=(
                    "Created on-demand by Von's chat file upload flow. Instances typically have blob store metadata "
                    "(URI, key, content type, size, and hash) recorded as text relations."
                ),
            )
            if not created.get("success"):
                current_app.logger.warning(
                    "[files/upload] Failed to create Computer File Copy type: %s",
                    created.get("message"),
                )
    except Exception as exc:
        current_app.logger.warning(
            f"[files/upload] KR type ensure failed (continuing): {exc}"
        )

    # --- Create the file-copy instance concept ---
    instance_concept_id = f"#V#uploaded_file_copy_{uuid.uuid4().hex}"

    try:
        from ...services import concept_service
        from ...db.repositories.concepts_repository import ConceptsRepository
        from ...services.text_value_service import upsert_text_for_concept

        instance = concept_service.create_concept(
            name=original_filename,
            concept_id=instance_concept_id,
            parent_concept_ids=[type_concept_id],
            create_as_instance=True,
            system_tags=["uploaded", "file", "blob_store"],
            attributes={
                "sha256": sha256,
                "size_bytes": size_bytes,
                "content_type": content_type,
                "blob_backend": blob_ref.backend,
                "blob_key": blob_ref.key,
                "blob_uri": blob_ref.uri,
            },
        )

        # Scope visibility to the current user.
        ConceptsRepository.update_one(
            {"concept_id": instance_concept_id},
            {"$set": {"relationships.specific_to_user": [user_concept_id.strip()]}},
        )

        # Attach blob + metadata as text relations (authoritative)
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_original_filename",
            text=original_filename,
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_sha256",
            text=sha256,
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_size_bytes",
            text=str(size_bytes),
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_upload_timestamp",
            text=str(uploaded_at),
            lang="en-NZ",
        )
        if content_type:
            upsert_text_for_concept(
                subject_concept_id=instance_concept_id,
                predicate="#V#has_mime_type",
                text=content_type,
                lang="en-NZ",
            )

        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_blob_backend",
            text=str(blob_ref.backend),
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_blob_key",
            text=str(blob_ref.key),
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_blob_uri",
            text=str(blob_ref.uri),
            lang="en-NZ",
        )
    except Exception as exc:
        current_app.logger.error(
            f"[files/upload] Failed to register uploaded file in Vontology: {exc}",
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "vontology_register_failed",
                    "detail": str(exc),
                }
            ),
            500,
        )

    chat_history_recorded = _record_file_upload_in_chat_history(
        user_concept_id=user_concept_id.strip(),
        session_id=session_id,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        blob_backend=str(blob_ref.backend),
        blob_key=str(blob_ref.key),
        blob_uri=str(blob_ref.uri),
        file_copy_concept_id=instance_concept_id,
    )

    return (
        jsonify(
            {
                "success": True,
                "uploaded": {
                    "concept_id": instance_concept_id,
                    "type_concept_id": type_concept_id,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "content_type": content_type,
                    "original_filename": original_filename,
                },
                "storage": {
                    "backend": blob_ref.backend,
                    "key": blob_ref.key,
                    "uri": blob_ref.uri,
                    "content_type": blob_ref.content_type,
                    "size_bytes": blob_ref.size_bytes,
                    "metadata": blob_ref.metadata,
                },
                "chat_history_recorded": bool(chat_history_recorded),
            }
        ),
        200,
    )


def _record_file_upload_in_chat_history(
    *,
    user_concept_id: str,
    session_id: str,
    original_filename: str,
    content_type: str | None,
    size_bytes: int,
    sha256: str,
    blob_backend: str,
    blob_key: str,
    blob_uri: str,
    file_copy_concept_id: str,
) -> bool:
    """Persist a durable upload record in chat history.

    We store human-readable text plus a compact JSON payload. This ensures the
    attachment can be rediscovered later (including the blob key/URI), assuming
    the user is authorised.
    """

    try:
        import json

        upload_summary = f"[UPLOAD] {original_filename} ({size_bytes} bytes)"
        assistant_lines: list[str] = [
            f"Attachment uploaded: {original_filename}",
            f"File copy concept: {file_copy_concept_id}",
            f"Blob URI: {blob_uri}",
            "(Blob access is subject to authorisation.)",
        ]

        payload = {
            "kind": "file_upload",
            "original_filename": original_filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "file_copy_concept_id": file_copy_concept_id,
            "blob": {
                "backend": blob_backend,
                "key": blob_key,
                "uri": blob_uri,
            },
        }

        assistant_text = (
            "\n".join(assistant_lines)
            + "\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )

        chat_history_service.add_message_to_history(
            user_concept_id,
            session_id,
            {"role": "user", "content": upload_summary},
        )
        chat_history_service.add_message_to_history(
            user_concept_id,
            session_id,
            {"role": "assistant", "content": assistant_text},
        )
        return True
    except Exception as exc:
        current_app.logger.warning(
            "[files/upload] Failed to record upload in chat history: %s", exc
        )
        return False


@von_bp.route("/api/files/<path:file_copy_concept_id>/download", methods=["GET"])
def download_file_copy(file_copy_concept_id: str):
    """Download an uploaded file-copy by its Vontology concept id.

    Security: user identity is derived server-side via get_effective_user_concept_id().
    Access is restricted using relationships.specific_to_user on the file-copy concept.

    Path params:
      - file_copy_concept_id: URL-encoded concept id (e.g. %23V%23uploaded_file_copy_...)
    Query params:
      - concept_id: optional override (for callers that prefer query param)
    """

    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return (
            jsonify(
                {
                    "success": False,
                    "error": "missing_user_context",
                    "message": "Missing user context: establish an authenticated session first.",
                }
            ),
            401,
        )

    # Allow query-parameter override so callers don't need to place the full id in the path.
    concept_id = request.args.get("concept_id") or file_copy_concept_id
    concept_id = str(concept_id or "").strip()
    if not concept_id:
        return jsonify({"success": False, "error": "missing_concept_id"}), 400

    try:
        from ...db.repositories.concepts_repository import ConceptsRepository

        concept_doc = ConceptsRepository.find_one({"concept_id": concept_id})
    except Exception as exc:
        current_app.logger.warning("[files/download] Concept lookup failed: %s", exc)
        concept_doc = None

    if not isinstance(concept_doc, dict):
        # Avoid leaking which concept IDs exist.
        return jsonify({"success": False, "error": "not_found"}), 404

    relationships = (
        concept_doc.get("relationships") if isinstance(concept_doc, dict) else None
    )
    specific = (
        relationships.get("specific_to_user")
        if isinstance(relationships, dict)
        else None
    )
    if isinstance(specific, list) and user_concept_id.strip() not in {
        str(x).strip() for x in specific if x is not None
    }:
        # Avoid leaking which concept IDs exist.
        return jsonify({"success": False, "error": "not_found"}), 404

    from ...services.computer_file_copy_service import fetch_file_copy_bytes

    result = fetch_file_copy_bytes(
        file_copy_concept_id=concept_id,
        allow_large=True,
        logger=current_app.logger,
    )
    if not isinstance(result, dict) or result.get("success") is not True:
        error = result.get("error") if isinstance(result, dict) else "not_found"
        if error == "not_found":
            return jsonify({"success": False, "error": "not_found"}), 404
        if error == "blob_fetch_failed":
            return jsonify({"success": False, "error": "blob_fetch_failed"}), 404
        if error == "file_too_large":
            return jsonify({"success": False, "error": "file_too_large"}), 413
        return jsonify({"success": False, "error": "not_found"}), 404

    info = result.get("info")
    data_bytes = result.get("data")
    if data_bytes is None:
        return jsonify({"success": False, "error": "blob_fetch_failed"}), 404

    import io

    download_name = "download"
    if info is not None and getattr(info, "original_filename", None):
        download_name = str(info.original_filename)
    else:
        name = concept_doc.get("name") if isinstance(concept_doc, dict) else None
        if isinstance(name, str) and name.strip():
            download_name = name.strip()

    mimetype = "application/octet-stream"
    if info is not None and getattr(info, "content_type", None):
        mimetype = str(info.content_type)
    resp = send_file(
        io.BytesIO(data_bytes),
        mimetype=mimetype,
        as_attachment=True,
        download_name=download_name,
        max_age=0,
    )
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _prune_tool_progress() -> None:
    cutoff = time.time() - _TOOL_PROGRESS_TTL_SEC
    with _TOOL_PROGRESS_LOCK:
        stale_keys = [
            key
            for key, value in _TOOL_PROGRESS.items()
            if isinstance(value, dict)
            and isinstance(value.get("updated_at_epoch"), (int, float))
            and float(value["updated_at_epoch"]) < cutoff
        ]
        for key in stale_keys:
            _TOOL_PROGRESS.pop(key, None)


def _set_tool_progress(scope_key: str, request_id: str, update: dict[str, Any]) -> None:
    _prune_tool_progress()
    now_epoch = time.time()
    with _TOOL_PROGRESS_LOCK:
        key = (scope_key, request_id)
        existing = _TOOL_PROGRESS.get(key)
        if not isinstance(existing, dict):
            existing = {}
        merged = {**existing, **(update or {})}
        merged["updated_at"] = _now_utc_iso()
        merged["updated_at_epoch"] = now_epoch
        _TOOL_PROGRESS[key] = merged


def _get_tool_progress(scope_key: str, request_id: str) -> dict[str, Any] | None:
    _prune_tool_progress()
    with _TOOL_PROGRESS_LOCK:
        value = _TOOL_PROGRESS.get((scope_key, request_id))
        return dict(value) if isinstance(value, dict) else None


def _clear_tool_progress(scope_key: str, request_id: str) -> None:
    with _TOOL_PROGRESS_LOCK:
        _TOOL_PROGRESS.pop((scope_key, request_id), None)


@von_bp.route("/progress/<request_id>", methods=["GET"])
def get_generation_progress(request_id: str):
    """Return the latest tool-execution progress for an in-flight generate() call."""

    if (
        not isinstance(request_id, str)
        or not request_id.strip()
        or len(request_id) > 200
    ):
        return jsonify({"error": "Invalid request_id"}), 400

    scope_key = _get_tool_progress_scope_key()
    state = _get_tool_progress(scope_key, request_id.strip())
    if not state:
        try:
            show_tool_use_progress = bool(get_show_tool_use_during_thinking())
        except Exception:
            show_tool_use_progress = False

        if not show_tool_use_progress:
            return jsonify({"status": "disabled"}), 200

        return jsonify({"status": "pending"}), 202

    # Do not leak internal epoch detail to the UI.
    state.pop("updated_at_epoch", None)
    return jsonify(state), 200


def _truncate_large_tool_results(
    messages: list[dict], max_tool_content_chars: int = 5000
) -> list[dict]:
    """
    Truncate large tool result content to prevent context explosion.

    Tool results from MCP can be very large (e.g., search results with hierarchies).
    This function limits the size of 'tool' role messages to prevent exponential
    token growth in the conversation context.

    Args:
        messages: List of message dictionaries
        max_tool_content_chars: Maximum characters to keep in tool message content

    Returns:
        New list with truncated tool messages
    """
    result = []
    for msg in messages:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > max_tool_content_chars:
                # Truncate and add indicator
                truncated_content = (
                    content[:max_tool_content_chars]
                    + f"\n... [truncated {len(content) - max_tool_content_chars} chars]"
                )
                result.append({**msg, "content": truncated_content})
            else:
                result.append(msg)
        else:
            result.append(msg)
    return result


def _truncate_debug_payload(raw: str | None, max_chars: int = 4000) -> str | None:
    """Limit rejected tool payloads before surfacing them in LLM debug info."""
    if raw is None:
        return None

    payload = raw if isinstance(raw, str) else str(raw)
    if len(payload) <= max_chars:
        return payload

    return payload[:max_chars] + f"\n... [truncated {len(payload) - max_chars} chars]"


def _limit_context_size(context: list[dict], max_messages: int = 20) -> list[dict]:
    """
    Keep only the most recent messages in context to prevent unbounded growth.

    Always preserves the system message (if present at index 0) and keeps the
    most recent user/assistant exchanges.

    Args:
        context: List of message dictionaries
        max_messages: Maximum number of messages to keep (excluding system message)

    Returns:
        Trimmed context list
    """
    if len(context) <= max_messages:
        return context

    # Check if first message is system message
    if context and context[0].get("role") == "system":
        system_msg = [context[0]]
        recent_msgs = context[-(max_messages - 1) :]  # Keep room for system message
        return system_msg + recent_msgs
    else:
        return context[-max_messages:]


_BUTTONIFY_PROMPT_IDS = ("#V#buttonify_prompt_v1",)


def _normalise_buttonify_option(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return None
    cleaned = cleaned.strip("-–—•*\t ")
    cleaned = re.sub(r"^[\"'“‘]+|[\"'”’]+$", "", cleaned).strip()
    cleaned = cleaned.rstrip(".,;:")
    if not cleaned:
        return None
    if len(cleaned) > 60:
        return None
    if len(cleaned.split()) > 4:
        return None
    return cleaned


def _add_buttonify_option(
    options: list[str],
    seen: set[str],
    value: str | None,
) -> None:
    cleaned = _normalise_buttonify_option(value)
    if not cleaned:
        return
    key = cleaned.lower()
    if key in seen:
        return
    seen.add(key)
    options.append(cleaned)


def _extract_buttonify_options_heuristic(text: str | None) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return []

    options: list[str] = []
    seen: set[str] = set()

    quote_patterns = [
        r'"([^"\n\r]{1,200})"',
        r"“([^”\n\r]{1,200})”",
        r"‘([^’\n\r]{1,200})’",
        r"(?<!\w)'([^'\n\r]{1,200})'(?!\w)",
    ]

    for pattern in quote_patterns:
        for match in re.findall(pattern, text):
            _add_buttonify_option(options, seen, match)
            if len(options) >= 4:
                return options[:4]

    for line in text.splitlines():
        match = re.match(r"\s*(?:[-*•]|\d+[.)])\s+(.+)", line)
        if not match:
            continue
        _add_buttonify_option(options, seen, match.group(1))
        if len(options) >= 4:
            return options[:4]

    if not options:
        marker = re.search(
            r"(?:reply|respond|answer|choose|pick)\s+(?:with\s+)?one\s+of\s*[:\-–—]?\s*(.+)",
            text,
            re.IGNORECASE,
        )
        if marker:
            tail = marker.group(1)
            for part in re.split(r"\s*(?:,|/|;|\bor\b)\s*", tail):
                _add_buttonify_option(options, seen, part)
                if len(options) >= 4:
                    return options[:4]

    implicit = re.search(
        r"\b(?:would|do)\s+(?:you\s+)?(?:like|want)\s+(?:to\s+)?([^?.!\n]{1,80}?)\s+or\s+([^?.!\n]{1,80}?)[?.!]",
        text,
        re.IGNORECASE,
    )
    if implicit:
        _add_buttonify_option(options, seen, implicit.group(1))
        _add_buttonify_option(options, seen, implicit.group(2))

    if not options:
        if re.search(r"\byes\s*/\s*no\b|\byes\s+or\s+no\b", text, re.IGNORECASE):
            _add_buttonify_option(options, seen, "Yes")
            _add_buttonify_option(options, seen, "No")
        else:
            trimmed = text.strip()
            if trimmed.endswith("?") and re.match(
                r"\s*(?:Do|Would|Is|Are|Did|Can|Should|Will|Have|Has)\b",
                trimmed,
                re.IGNORECASE,
            ):
                _add_buttonify_option(options, seen, "Yes")
                _add_buttonify_option(options, seen, "No")

    return options[:4]


def _calculate_context_stats(messages: list[dict]) -> dict:
    """
    Calculate statistics about message context for debugging.

    Args:
        messages: List of message dictionaries

    Returns:
        Dictionary with context statistics
    """
    stats = {
        "total_messages": len(messages),
        "by_role": {},
        "total_chars": 0,
        "largest_message": {"role": None, "chars": 0},
    }

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        content_len = len(str(content))

        # Count by role
        stats["by_role"][role] = stats["by_role"].get(role, 0) + 1

        # Total characters
        stats["total_chars"] += content_len

        # Track largest message
        if content_len > stats["largest_message"]["chars"]:
            stats["largest_message"] = {"role": role, "chars": content_len}

    return stats


def _calculate_tool_stats(tool_messages: list[dict]) -> dict:
    """
    Calculate statistics about MCP tool results.

    Args:
        tool_messages: List of tool message dictionaries

    Returns:
        Dictionary with tool result statistics
    """
    stats = {
        "tool_count": len(tool_messages),
        "total_chars": 0,
        "truncated_count": 0,
        "tools": [],
    }

    for msg in tool_messages:
        if msg.get("role") != "tool":
            continue

        content = msg.get("content", "")
        content_str = str(content)
        content_len = len(content_str)

        stats["total_chars"] += content_len

        # Check if truncated
        was_truncated = "[truncated" in content_str
        if was_truncated:
            stats["truncated_count"] += 1

        # Try to parse tool name from content
        tool_name = "unknown"
        try:
            import json

            parsed = json.loads(
                content_str.split("[truncated")[0] if was_truncated else content_str
            )
            if isinstance(parsed, dict):
                tool_name = parsed.get("tool", "unknown")
        except:
            pass

        stats["tools"].append(
            {"name": tool_name, "chars": content_len, "truncated": was_truncated}
        )

    return stats


def _derive_llm_debug_warnings(debug_info: dict) -> list[str]:
    """
    Derive warnings from LLM debug information.

    Mirrors the frontend deriveLlmDebugWarnings logic to ensure backend
    warnings are persisted in the JSON structure.

    Args:
        debug_info: The llm_debug_info dictionary

    Returns:
        List of warning strings
    """
    warnings = []

    if not debug_info or not isinstance(debug_info, dict):
        return warnings

    # Check for backend errors
    if isinstance(debug_info.get("error"), str) and debug_info.get("error", "").strip():
        warnings.append(f"Backend error: {debug_info['error'].strip()}")

    # Check auxiliary LLM calls for warnings
    aux_calls = debug_info.get("aux_llm_calls", [])
    if isinstance(aux_calls, list):
        for call in aux_calls:
            if not isinstance(call, dict):
                continue

            call_type = call.get("type", "")

            # Check missing tool-call classifier warnings
            if call_type == "missing_tool_call_classifier":
                injection_mode = call.get("prompt_injection_mode", "")
                if injection_mode == "append":
                    warnings.append(
                        "Missing tool-call detector prompt did not include `{response}` placeholder; response was appended."
                    )

                verdict = str(call.get("response_preview", "")).strip().lower()
                if verdict and not verdict.startswith(("yes", "no")):
                    warnings.append(
                        "Missing tool-call classifier returned an unexpected verdict (not yes/no)."
                    )

                model_raw = call.get("model_raw", "")
                model_resolved = call.get("model_resolved", "")
                if (
                    isinstance(model_raw, str)
                    and model_raw.startswith("#V#")
                    and not model_resolved
                ):
                    warnings.append(
                        "Missing tool-call classifier model could not be resolved from ontology ID."
                    )

            # Check for call-level errors
            if isinstance(call.get("error"), str) and call.get("error", "").strip():
                warnings.append(call["error"].strip())

    # Tool invocation failures / parse errors
    tool_invocations = debug_info.get("tool_invocations", [])
    if isinstance(tool_invocations, list):
        for inv in tool_invocations:
            if not isinstance(inv, dict):
                continue
            method = inv.get("method")
            if not isinstance(method, str):
                method = (
                    inv.get("tool") if isinstance(inv.get("tool"), str) else "unknown"
                )
            error = inv.get("error")
            error_text = error.strip() if isinstance(error, str) else ""
            if not error_text:
                continue

            # Surface parse errors directly so it is obvious tools were not executed.
            if method == "__tool_call_parse_error__":
                warnings.append(error_text)
            else:
                warnings.append(f"Tool {method} failed: {error_text}")

    # Max tool invocation cap reached (LLM still wants tools)
    try:
        internal_mcp = debug_info.get("internal_mcp")
        caps = (
            internal_mcp.get("execution_caps")
            if isinstance(internal_mcp, dict)
            else None
        )
        max_invocations = (
            caps.get("max_tool_invocations") if isinstance(caps, dict) else None
        )
        max_invocations = int(max_invocations) if max_invocations is not None else None
    except Exception:
        max_invocations = None

    response_text = debug_info.get("response")
    if (
        isinstance(response_text, str)
        and isinstance(max_invocations, int)
        and max_invocations > 0
        and isinstance(tool_invocations, list)
        and len(tool_invocations) >= max_invocations
    ):
        trimmed = response_text.strip()
        looks_like_tool_call = (
            (trimmed.startswith("{") or trimmed.startswith("["))
            and '"call_tool"' in trimmed
            and '"tool"' in trimmed
        )
        if looks_like_tool_call:
            warnings.append(
                f"Reached max tool invocation limit ({max_invocations}); additional tool calls were not executed."
            )

    # Check presenter channel health (screen/spoken routes)
    presenter_channels = debug_info.get("presenter_channels")
    if isinstance(presenter_channels, dict):
        screen_value = presenter_channels.get("screen")
        spoken_value = presenter_channels.get("spoken")
        screen_ok = isinstance(screen_value, str) and bool(screen_value.strip())
        spoken_ok = isinstance(spoken_value, str) and bool(spoken_value.strip())

        # If one channel is missing, flag it so the other route output acts as a
        # diagnostic cue (without mutating the actual output text).
        if screen_ok and not spoken_ok:
            warnings.append(
                "Presenter output missing spoken channel; text-to-speech will fall back to screen text."
            )
        elif spoken_ok and not screen_ok:
            warnings.append(
                "Presenter output missing screen channel; display will fall back to spoken text."
            )
        elif not screen_ok and not spoken_ok:
            warnings.append(
                "Presenter output present but both screen and spoken channels are empty."
            )

    spoken_backfill_attempted = bool(
        debug_info.get("spoken_backfill_second_pass_attempted")
    )
    spoken_backfill_reason = debug_info.get("spoken_backfill_second_pass_reason")
    if spoken_backfill_attempted:
        # Flag only if spoken is still missing after backfill attempt.
        spoken_still_missing = True
        if isinstance(presenter_channels, dict):
            spoken_value = presenter_channels.get("spoken")
            spoken_still_missing = not (
                isinstance(spoken_value, str) and bool(spoken_value.strip())
            )

        if spoken_still_missing:
            reason_text = (
                str(spoken_backfill_reason).strip()
                if isinstance(spoken_backfill_reason, str)
                and spoken_backfill_reason.strip()
                else "unknown_reason"
            )
            warnings.append(
                f"Spoken backfill attempted but spoken channel is still missing ({reason_text})."
            )

    # Remove duplicates while preserving order
    seen = set()
    unique_warnings = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            unique_warnings.append(w)

    return unique_warnings


def _strip_fenced_code_blocks(text: str) -> str:
    if not isinstance(text, str) or not text:
        return ""
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def _extract_presenter_channels(text: str) -> dict[str, object] | None:
    """Extract presenter-style output blocks.

    Expected format (v1):
      <spoken>...talk track...</spoken>
      <screen>...what to display...</screen>

    Returns None when no tags are present.
    """

    if not isinstance(text, str) or not text:
        return None

    searchable = _strip_fenced_code_blocks(text)

    def _find_block(tag: str) -> str | None:
        pattern = rf"<{tag}>\s*(.*?)\s*</{tag}>"
        match = re.search(pattern, searchable, flags=re.DOTALL | re.IGNORECASE)
        if not match:
            return None
        value = match.group(1)
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value if value else None

    spoken = _find_block("spoken")
    screen = _find_block("screen")

    if spoken is None and screen is None:
        return None

    # Defaults:
    # - If only <spoken> is provided, display it on-screen too (otherwise we'd render the raw tags).
    # - If only <screen> is provided, do NOT fabricate spoken from screen; let the client fall back.
    screen_text = screen if screen is not None else spoken
    spoken_text = spoken

    if screen_text is None:
        screen_text = text.strip()

    return {
        "format": "tagged_blocks_v1",
        "extracted": True,
        "screen": screen_text,
        "spoken": spoken_text,
    }


def _presenter_tag_present(text: str, tag: str) -> bool:
    if not isinstance(text, str) or not text:
        return False
    pattern = rf"<{tag}>\s*(.*?)\s*</{tag}>"
    searchable = _strip_fenced_code_blocks(text)
    return bool(re.search(pattern, searchable, flags=re.DOTALL | re.IGNORECASE))


def _build_presenter_screen_from_tool_messages(
    tool_messages: list[dict],
) -> str | None:
    if not tool_messages:
        return None

    import json

    entries: list[str] = []
    index = 0
    for msg in tool_messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        index += 1
        parsed = None
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = None

        if isinstance(parsed, dict):
            tool_name = parsed.get("tool") or parsed.get("method") or "tool"
            status = parsed.get("status")
            duration_ms = parsed.get("duration_ms")
            error = parsed.get("error")
            payload = parsed.get("payload")

            lines = [f"{index}. {tool_name}"]
            if status:
                lines.append(f"Status: {status}")
            if duration_ms is not None:
                lines.append(f"Duration: {duration_ms} ms")
            if error:
                lines.append(f"Error: {error}")
            if payload is not None:
                try:
                    payload_text = json.dumps(
                        payload,
                        indent=2,
                        sort_keys=True,
                        ensure_ascii=True,
                    )
                except Exception:
                    payload_text = str(payload)
                lines.append("Payload:")
                lines.append("```json")
                lines.append(payload_text)
                lines.append("```")
            entries.append("\n".join(lines))
        else:
            raw = content.strip()
            entries.append(f"{index}. Tool result\n```\n{raw}\n```")

    if not entries:
        return None

    return "Tool results:\n\n" + "\n\n".join(entries)


def _extract_screen_only(text: str) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    searchable = _strip_fenced_code_blocks(text)
    m = re.search(
        r"<screen>\s*(.*?)\s*</screen>", searchable, flags=re.DOTALL | re.IGNORECASE
    )
    if not m:
        return None
    value = m.group(1)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _build_presenter_screen_summary_from_tool_messages(
    tool_messages: list[dict],
) -> str | None:
    """Deterministic, user-facing summary of tool activity.

    This deliberately avoids embedding raw JSON payloads so it can safely appear
    in the main chat transcript.
    """

    if not tool_messages:
        return None

    import json

    lines: list[str] = []
    lines.append("Tools ran to answer this request:")

    description_write_seen = False
    relationship_write_seen = False
    names_write_seen = False
    concept_create_seen = False

    index = 0
    for msg in tool_messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        parsed = None
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = None

        if not isinstance(parsed, dict):
            index += 1
            lines.append(f"{index}. Tool result (unstructured)")
            continue

        tool_name = parsed.get("tool") or parsed.get("method") or "tool"
        status = parsed.get("status")
        error = parsed.get("error")
        payload = parsed.get("payload")

        tool_name_text = tool_name.strip() if isinstance(tool_name, str) else ""
        tool_name_lower = tool_name_text.lower()
        payload_dict = payload if isinstance(payload, dict) else {}

        if "description" in tool_name_lower:
            description_write_seen = True
        predicate = payload_dict.get("predicate")
        if isinstance(predicate, str) and predicate.strip() in {
            "hasDescription",
            "has_description",
            "#V#hasDescription",
        }:
            description_write_seen = True
        description_value = payload_dict.get("description")
        if isinstance(description_value, str) and description_value.strip():
            description_write_seen = True

        if tool_name_lower in {"add_relationship", "remove_relationship"}:
            relationship_write_seen = True
        if tool_name_lower in {"add_names", "add_name"}:
            names_write_seen = True
        if tool_name_lower in {"create_concepts", "create_concept"}:
            concept_create_seen = True

        index += 1
        entry = f"{index}. {tool_name}"
        if status:
            entry += f" — {status}"
        lines.append(entry)
        if error:
            lines.append(f"   Error: {error}")

        # Pull out a few common, safe identifiers to help the user.
        if isinstance(payload, dict):
            for key in ("concept_id", "identifier", "id", "url", "arxiv_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    lines.append(f"   {key}: {value.strip()}")

    if index == 0:
        return None

    # Always include an explicit writes ledger to make negative facts visible.
    lines.append("")
    lines.append("Writes ledger (authoritative):")
    if description_write_seen:
        lines.append("- Description updated: YES (evidence present in tool results)")
    else:
        lines.append("- Description updated: NO (no description write tool ran)")

    did_not_lines: list[str] = []
    if not relationship_write_seen:
        did_not_lines.append("- No relationship writes detected")
    if not names_write_seen:
        did_not_lines.append("- No name writes detected")
    if not concept_create_seen:
        did_not_lines.append("- No concept creation detected")

    if did_not_lines:
        lines.append("")
        lines.append("Writes not detected:")
        lines.extend(did_not_lines)

    return "\n".join(lines).strip() or None


def _build_tool_messages_prompt_blob(
    tool_messages: list[dict], *, max_chars: int = 12000
) -> str:
    """Build a compact plain-text representation of tool results for LLM backfill."""

    if not tool_messages:
        return "(no tool messages)"

    import json

    def _normalise_tool_name(parsed: dict) -> str:
        tool_name = (
            parsed.get("tool") or parsed.get("method") or parsed.get("name") or ""
        )
        return tool_name.strip() if isinstance(tool_name, str) else ""

    def _iter_parsed_tool_results(messages: list[dict]) -> list[dict]:
        parsed_results: list[dict] = []
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            try:
                parsed = json.loads(content)
            except Exception:
                continue
            if isinstance(parsed, dict):
                parsed_results.append(parsed)
        return parsed_results

    parsed_results = _iter_parsed_tool_results(tool_messages)

    executed_lines: list[str] = []
    writes_lines: list[str] = []

    # Track a small set of write categories we care about for UI truthfulness.
    description_write_seen = False
    relationship_write_seen = False
    names_write_seen = False
    concept_create_seen = False

    def _mark_description_write(tool_name: str, payload: dict) -> None:
        nonlocal description_write_seen
        if description_write_seen:
            return
        tool_name_lower = (tool_name or "").lower()
        if "description" in tool_name_lower:
            description_write_seen = True
            return
        predicate = payload.get("predicate")
        if isinstance(predicate, str) and predicate.strip() in {
            "hasDescription",
            "has_description",
            "#V#hasDescription",
        }:
            description_write_seen = True
            return
        description_value = payload.get("description")
        if isinstance(description_value, str) and description_value.strip():
            description_write_seen = True

    def _summarise_relationship_write(tool_name: str, payload: dict) -> str | None:
        nonlocal relationship_write_seen
        name_lower = (tool_name or "").lower()
        if name_lower not in {"add_relationship", "remove_relationship"}:
            return None

        source_id = payload.get("source_id")
        predicate = payload.get("predicate")
        target = payload.get("target")
        added = payload.get("added")
        removed = payload.get("removed")

        if not (
            isinstance(source_id, str)
            and isinstance(predicate, str)
            and isinstance(target, str)
        ):
            relationship_write_seen = True
            return f"- Relationship update via {tool_name} (details unavailable)"

        relationship_write_seen = True

        verb = "changed"
        if name_lower == "add_relationship":
            verb = "added" if added is not False else "attempted"
        elif name_lower == "remove_relationship":
            verb = "removed" if removed is not False else "attempted"

        return f"- Relationship {verb}: `{source_id}` — `{predicate}` → `{target}`"

    def _summarise_name_or_concept_write(tool_name: str, payload: dict) -> str | None:
        nonlocal names_write_seen, concept_create_seen
        name_lower = (tool_name or "").lower()
        if name_lower in {"add_names", "add_name"}:
            names_write_seen = True
            concept_id = payload.get("concept_id")
            if isinstance(concept_id, str) and concept_id.strip():
                return f"- Names added for `{concept_id}`"
            return "- Names added"

        if name_lower in {"create_concepts", "create_concept"}:
            concept_create_seen = True
            total = payload.get("total")
            if isinstance(total, int):
                return f"- Concepts created: {total}"
            return "- Concept creation attempted"

        return None

    for parsed in parsed_results:
        tool_name = _normalise_tool_name(parsed) or "tool"
        status = parsed.get("status")
        status_text = (
            status.strip() if isinstance(status, str) and status.strip() else None
        )
        executed_lines.append(
            f"- {tool_name}" + (f" ({status_text})" if status_text else "")
        )

        payload = parsed.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        # Categorise writes.
        rel_summary = _summarise_relationship_write(tool_name, payload)
        if rel_summary:
            writes_lines.append(rel_summary)

        name_or_concept_summary = _summarise_name_or_concept_write(tool_name, payload)
        if name_or_concept_summary:
            writes_lines.append(name_or_concept_summary)

        _mark_description_write(tool_name, payload)

    # Always include an explicit description verdict because it is a common source of confusion.
    if description_write_seen:
        writes_lines.append(
            "- Description updated: YES (evidence present in tool results)"
        )
    else:
        writes_lines.append("- Description updated: NO (no description write tool ran)")

    # Provide a small "did not happen" block to make negative facts explicit.
    did_not_lines: list[str] = []
    if not relationship_write_seen:
        did_not_lines.append("- No relationship writes detected")
    if not names_write_seen:
        did_not_lines.append("- No name writes detected")
    if not concept_create_seen:
        did_not_lines.append("- No concept creation detected")

    blob_lines: list[str] = []
    blob_lines.append("TOOL EXECUTION (authoritative):")
    blob_lines.extend(executed_lines or ["- (no parsed tool results)"])
    blob_lines.append("")
    blob_lines.append("TOOL WRITES LEDGER (authoritative):")
    blob_lines.extend(writes_lines or ["- No writes detected"])
    if did_not_lines:
        blob_lines.append("")
        blob_lines.append("WRITES NOT DETECTED:")
        blob_lines.extend(did_not_lines)

    blob = "\n".join(blob_lines).strip() or "(tool results unavailable)"

    blob = str(blob)
    if len(blob) > max_chars:
        blob = blob[:max_chars].rstrip() + "\n... [truncated]"
    return blob


def _is_prompt_introspection_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    triggers = (
        "what is my user prompt",
        "what's my user prompt",
        "what is my system prompt",
        "what's my system prompt",
        "what prompt is active",
        "which prompt is active",
        "tell me what my user prompt is",
        "tell me what my system prompt is",
    )
    return any(t in lowered for t in triggers)


def _is_tool_introspection_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    triggers = (
        "what tools do you have",
        "what tools can you",
        "what tools can i",
        "list tools",
        "show tools",
        "available tools",
        "tool list",
        "mcp tools",
        "what can you do",
        "what capabilities do you have",
    )
    return any(t in lowered for t in triggers)


def _is_rag_status_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    triggers = (
        "rag status",
        "what is my rag status",
        "is rag enabled",
        "is rag on",
        "rag enabled",
        "rag on",
        "rag working",
        "rag isolation",
    )
    return any(t in lowered for t in triggers)


def _format_tool_inventory(methods: dict) -> str:
    # Deterministic plain-text listing; keep stable ordering.
    if not isinstance(methods, dict) or not methods:
        return "No internal MCP tools are registered."

    # Group by category
    buckets: dict[str, list[tuple[str, str]]] = {}
    for name, meta in methods.items():
        if not isinstance(name, str):
            continue
        meta_dict = meta if isinstance(meta, dict) else {}

        category: str = "other"
        category_value = meta_dict.get("category")
        if isinstance(category_value, str) and category_value.strip():
            category = category_value.strip()

        desc: str = ""
        desc_value = meta_dict.get("description")
        if isinstance(desc_value, str):
            desc = desc_value.strip()

        buckets.setdefault(category, []).append((name, desc))

    lines: list[str] = []
    lines.append("Internal MCP tools currently registered:")
    for category in sorted(buckets.keys()):
        lines.append("")
        lines.append(f"- {category}:")
        for name, desc in sorted(buckets[category], key=lambda x: x[0]):
            if desc:
                lines.append(f"  - {name}: {desc}")
            else:
                lines.append(f"  - {name}")
    lines.append("")
    lines.append(
        "Note: Some tools (especially RAG) require a user namespace to avoid cross-user data leakage."
    )
    return "\n".join(lines)


def _deterministic_introspection_enabled() -> bool:
    try:
        flag_value = os.getenv("VON_DETERMINISTIC_INTROSPECTION", "0")
        return str(flag_value).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def _maybe_handle_prompt_introspection_fastpath(
    *,
    prompt_text: str,
    user_concept_id: str,
    session_id: str,
    auxiliary_system_prompt: str | None,
    user_prompt_debug: dict,
    context: list[dict],
    interaction_timestamp_utc: str,
    model_name: str,
    request_start_perf: float,
):
    gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
    import json as _json

    tool_messages: list[dict] = []
    tool_invocations: list[dict] = []

    response_text = None
    used_tool = False

    if gateway is not None and getattr(gateway, "enabled", False):
        try:
            tool_result = gateway.invoke(
                "chat_get_prompt_context",
                {
                    "namespace": user_concept_id,
                    "include_content": True,
                    "max_chars": 5000,
                },
            )
            payload = tool_result.payload
            duration_ms = getattr(tool_result, "duration_ms", None)
            used_tool = True

            tool_messages = [
                {
                    "role": "tool",
                    "content": _json.dumps(
                        {
                            "tool": "chat_get_prompt_context",
                            "status": "ok",
                            "duration_ms": duration_ms,
                            "payload": payload,
                        },
                        default=str,
                    ),
                }
            ]
            tool_invocations = [
                {
                    "tool": "chat_get_prompt_context",
                    "payload": {
                        "namespace": user_concept_id,
                        "include_content": True,
                        "max_chars": 5000,
                    },
                    "duration_ms": duration_ms,
                    "direct_user_call": False,
                }
            ]

            if isinstance(payload, dict) and payload.get("success"):
                prompt_ids = payload.get("prompt_concept_ids") or []
                prompt_text_value = payload.get("prompt_text") or ""
                if not isinstance(prompt_text_value, str):
                    prompt_text_value = str(prompt_text_value)
                prompt_text_value = prompt_text_value.strip()
                if not prompt_text_value:
                    response_text = (
                        "No user-specific system prompt text is currently available for your account. "
                        "(The prompt linkage exists but no content was returned.)"
                    )
                else:
                    response_text = (
                        "Here is your current user-specific system prompt (from Vontology).\n\n"
                        f"Prompt concept IDs: {prompt_ids}\n\n"
                        f"{prompt_text_value}"
                    )
            else:
                response_text = (
                    "I could not retrieve your user-specific prompt context via internal tools. "
                    f"Result: {payload}"
                )
        except Exception as exc:
            current_app.logger.warning(
                "[mcp_orchestrator] Prompt introspection tool failed: %s", exc
            )

    # Fallback: use already-loaded prompt fragments (no tool required)
    if response_text is None:
        prompt_ids = (
            user_prompt_debug.get("prompt_concept_ids")
            if isinstance(user_prompt_debug, dict)
            else []
        )
        if not isinstance(prompt_ids, list):
            prompt_ids = []
        prompt_text_value = auxiliary_system_prompt or ""
        prompt_text_value = (
            prompt_text_value.strip()
            if isinstance(prompt_text_value, str)
            else str(prompt_text_value)
        )
        if not prompt_text_value:
            response_text = "No user-specific system prompt is currently active (no prompt content was loaded from Vontology)."
        else:
            response_text = (
                "Here is your current user-specific system prompt (from Vontology).\n\n"
                f"Prompt concept IDs: {prompt_ids}\n\n"
                f"{prompt_text_value}"
            )

    # Store messages in history/context, matching the direct-tool-call pattern.
    chat_history_service.add_message_to_history(
        user_concept_id,
        session_id,
        {"role": "user", "content": prompt_text},
    )
    for tool_msg in _truncate_large_tool_results(
        tool_messages, max_tool_content_chars=5000
    ):
        chat_history_service.add_message_to_history(
            user_concept_id, session_id, tool_msg
        )
    chat_history_service.add_message_to_history(
        user_concept_id,
        session_id,
        {"role": "assistant", "content": response_text},
    )

    current_app.config["CONTEXT"] = _limit_context_size(
        current_app.config.get("CONTEXT", []), max_messages=20
    )

    current_turn_messages = [{"role": "user", "content": prompt_text}] + tool_messages
    context_stats = _calculate_context_stats(context)
    current_context_stats = _calculate_context_stats(current_app.config["CONTEXT"])
    tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None

    llm_debug_info = {
        "interaction_timestamp_utc": interaction_timestamp_utc,
        "model": model_name,
        "llm_interaction": {
            "requested_model": model_name,
            "orchestrator_used": False,
            "duration_ms": None,
            "usage": None,
            "calls": [],
            "server_elapsed_ms": (time.perf_counter() - request_start_perf) * 1000.0,
        },
        "messages": current_turn_messages,
        "response": response_text,
        "user_prompt": user_prompt_debug,
        "context_stats": {
            "sent_to_llm": context_stats,
            "stored_context": current_context_stats,
        },
        "tool_stats": tool_stats,
        "tool_invocations": tool_invocations,
        "prompt_introspection_fastpath": {"used_tool": used_tool, "enabled": True},
        "fastpath": {
            "name": "prompt_introspection",
            "bypassed_llm": True,
            "used_tool": used_tool,
            "enabled": True,
        },
    }
    llm_debug_info["warnings"] = _derive_llm_debug_warnings(llm_debug_info)

    return jsonify(
        {
            "response": response_text,
            "fastpath": {
                "name": "prompt_introspection",
                "bypassed_llm": True,
                "used_tool": used_tool,
            },
            "llm_debug": llm_debug_info,
        }
    )


def _maybe_handle_tool_inventory_fastpath(
    *,
    prompt_text: str,
    user_concept_id: str | None,
    session_id: str,
    context: list[dict],
    interaction_timestamp_utc: str,
    model_name: str,
    request_start_perf: float,
    user_prompt_debug: dict,
):
    gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")

    response_text = None
    used_tool = False
    methods_snapshot = None

    if gateway is not None and getattr(gateway, "enabled", False):
        try:
            methods_snapshot = gateway.describe_methods()
            used_tool = True
            response_text = _format_tool_inventory(methods_snapshot)
        except Exception as exc:
            response_text = (
                f"Could not retrieve tool inventory from the internal gateway: {exc}"
            )
    else:
        response_text = (
            "Internal MCP gateway is disabled; tool inventory is unavailable."
        )

    current_turn_messages = [{"role": "user", "content": prompt_text}]
    context_stats = _calculate_context_stats(context)
    current_context_stats = _calculate_context_stats(
        current_app.config.get("CONTEXT", [])
    )

    llm_debug_info = {
        "interaction_timestamp_utc": interaction_timestamp_utc,
        "model": model_name,
        "llm_interaction": {
            "requested_model": model_name,
            "orchestrator_used": False,
            "duration_ms": None,
            "usage": None,
            "calls": [],
            "server_elapsed_ms": (time.perf_counter() - request_start_perf) * 1000.0,
        },
        "messages": current_turn_messages,
        "response": response_text,
        "user_prompt": user_prompt_debug,
        "context_stats": {
            "sent_to_llm": context_stats,
            "stored_context": current_context_stats,
        },
        "tool_stats": None,
        "tool_invocations": (
            [
                {
                    "tool": "gateway.describe_methods",
                    "payload": {},
                    "duration_ms": None,
                    "direct_user_call": False,
                    "ok": bool(methods_snapshot is not None),
                }
            ]
            if used_tool
            else []
        ),
        "fastpath": {
            "name": "tool_inventory",
            "bypassed_llm": True,
            "used_tool": used_tool,
            "enabled": True,
        },
    }

    llm_debug_info["warnings"] = _derive_llm_debug_warnings(llm_debug_info)

    if user_concept_id:
        chat_history_service.add_message_to_history(
            user_concept_id,
            session_id,
            {"role": "user", "content": prompt_text},
        )
        chat_history_service.add_message_to_history(
            user_concept_id,
            session_id,
            {"role": "assistant", "content": response_text},
        )
    else:
        stored_context = current_app.config.get("CONTEXT", [])
        stored_context.append({"role": "user", "content": prompt_text})
        stored_context.append({"role": "assistant", "content": response_text})
        current_app.config["CONTEXT"] = _limit_context_size(
            stored_context, max_messages=20
        )

    current_app.config["CONTEXT"] = _limit_context_size(
        current_app.config.get("CONTEXT", []), max_messages=20
    )

    return jsonify(
        {
            "response": response_text,
            "fastpath": {
                "name": "tool_inventory",
                "bypassed_llm": True,
                "used_tool": used_tool,
            },
            "llm_debug": llm_debug_info,
        }
    )


def _maybe_handle_rag_status_fastpath(
    *,
    prompt_text: str,
    user_concept_id: str,
    session_id: str,
    context: list[dict],
    interaction_timestamp_utc: str,
    model_name: str,
    request_start_perf: float,
    user_prompt_debug: dict,
):
    gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
    import json as _json

    tool_messages: list[dict] = []
    tool_invocations: list[dict] = []
    used_tool = False
    response_text = None

    if gateway is not None and getattr(gateway, "enabled", False):
        try:
            tool_result = gateway.invoke(
                "rag_get_status",
                {"namespace": user_concept_id},
            )
            payload = tool_result.payload
            duration_ms = getattr(tool_result, "duration_ms", None)
            used_tool = True

            tool_messages = [
                {
                    "role": "tool",
                    "content": _json.dumps(
                        {
                            "tool": "rag_get_status",
                            "status": "ok",
                            "duration_ms": duration_ms,
                            "payload": payload,
                        },
                        default=str,
                    ),
                }
            ]
            tool_invocations = [
                {
                    "tool": "rag_get_status",
                    "payload": {"namespace": user_concept_id},
                    "duration_ms": duration_ms,
                    "direct_user_call": False,
                }
            ]

            response_text = (
                "Here is your current RAG status (server-truth):\n\n"
                + _json.dumps(payload, indent=2, default=str)
            )
        except Exception as exc:
            response_text = f"I could not retrieve RAG status via internal tools: {exc}"
    else:
        response_text = "Internal MCP gateway is disabled; RAG status is unavailable."

    # Persist messages in history/context.
    chat_history_service.add_message_to_history(
        user_concept_id,
        session_id,
        {"role": "user", "content": prompt_text},
    )
    for tool_msg in _truncate_large_tool_results(
        tool_messages, max_tool_content_chars=5000
    ):
        chat_history_service.add_message_to_history(
            user_concept_id, session_id, tool_msg
        )
    chat_history_service.add_message_to_history(
        user_concept_id,
        session_id,
        {"role": "assistant", "content": response_text},
    )

    current_app.config["CONTEXT"] = _limit_context_size(
        current_app.config.get("CONTEXT", []), max_messages=20
    )

    current_turn_messages = [{"role": "user", "content": prompt_text}] + tool_messages
    context_stats = _calculate_context_stats(context)
    current_context_stats = _calculate_context_stats(current_app.config["CONTEXT"])
    tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None

    llm_debug_info = {
        "interaction_timestamp_utc": interaction_timestamp_utc,
        "model": model_name,
        "llm_interaction": {
            "requested_model": model_name,
            "orchestrator_used": False,
            "duration_ms": None,
            "usage": None,
            "calls": [],
            "server_elapsed_ms": (time.perf_counter() - request_start_perf) * 1000.0,
        },
        "messages": current_turn_messages,
        "response": response_text,
        "user_prompt": user_prompt_debug,
        "context_stats": {
            "sent_to_llm": context_stats,
            "stored_context": current_context_stats,
        },
        "tool_stats": tool_stats,
        "tool_invocations": tool_invocations,
        "fastpath": {
            "name": "rag_status",
            "bypassed_llm": True,
            "used_tool": used_tool,
            "enabled": True,
        },
    }
    llm_debug_info["warnings"] = _derive_llm_debug_warnings(llm_debug_info)

    return jsonify(
        {
            "response": response_text,
            "fastpath": {
                "name": "rag_status",
                "bypassed_llm": True,
                "used_tool": used_tool,
            },
            "llm_debug": llm_debug_info,
        }
    )


@von_bp.route("/onboard_new_member", methods=["POST"])
def onboard_new_member():
    """Onboards a new lab member."""
    data = request.get_json()
    member_name = data.get("member_name")

    if not member_name:
        return jsonify({"error": "No member name provided."}), 400

    try:
        run_onboarding_workflow(member_name)
        return (
            jsonify({"message": f"Onboarding workflow started for {member_name}."}),
            200,
        )
    except Exception as e:
        print(f"Error during onboarding: {e}")  # Add server-side logging
        return jsonify({"error": f"Error during onboarding: {str(e)}"}), 500


@von_bp.route("/update_model", methods=["POST"])
def update_model():
    """Update the model based on user input."""
    data = request.get_json()
    new_model = data.get("model")

    if new_model:
        current_app.config["MODEL"] = new_model
        print(f"Model updated to: {new_model}")  # Add server-side logging
        return jsonify({"message": f"Model updated to {new_model}"}), 200
    else:
        return jsonify({"error": "No model provided."}), 400


@von_bp.route("/")
def serve_page():
    """Serve the main chat interface (HTML file)."""
    try:
        # Ensure template changes (e.g., recent fixes) are picked up even in production mode
        jenv = getattr(current_app, "jinja_env", None)
        cache = getattr(jenv, "cache", None)
        clear_fn = getattr(cache, "clear", None)
        if callable(clear_fn):
            clear_fn()
    except Exception:
        pass  # Defensive: don't block page serving if cache clear fails
    return render_template("von_interface.html")


@von_bp.route("/generate", methods=["POST"])
def generate():  # pyright: ignore[reportGeneralTypeIssues]
    """Handle text generation requests."""
    data = request.get_json()
    prompt_text = data.get("prompt", "")

    client_request_id = data.get("client_request_id")
    if (
        isinstance(client_request_id, str)
        and client_request_id.strip()
        and len(client_request_id) <= 200
    ):
        request_id = client_request_id.strip()
    else:
        request_id = str(uuid.uuid4())

    presenter_mode_requested = bool(data.get("presenter_mode"))

    request_start_perf = time.perf_counter()

    interaction_timestamp_utc = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    # Get user/org context from request body (sent by frontend from localStorage)
    request_user_id = data.get("user_id")
    request_org_id = data.get("org_id")
    request_language = data.get("language", "en-NZ")
    request_gmail_profile_raw = data.get("gmail_profile")
    request_gmail_profile = None
    if isinstance(request_gmail_profile_raw, str):
        request_gmail_profile = request_gmail_profile_raw.strip() or None
    elif request_gmail_profile_raw is not None:
        return (
            jsonify(
                {
                    "error": "invalid_gmail_profile",
                    "detail": "gmail_profile must be a string.",
                }
            ),
            400,
        )

    if request_gmail_profile:
        from ...integrations.google.gmail_service import list_profile_ids_from_env

        available_profiles = list_profile_ids_from_env()
        if not available_profiles:
            return (
                jsonify(
                    {
                        "error": "gmail_profiles_not_configured",
                        "detail": "No Gmail profiles are configured on the server.",
                    }
                ),
                400,
            )
        if request_gmail_profile not in available_profiles:
            return (
                jsonify(
                    {
                        "error": "unknown_gmail_profile",
                        "detail": "gmail_profile is not configured on the server.",
                        "available_profiles": available_profiles,
                    }
                ),
                400,
            )

    # Session management
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    session_id = session["session_id"]

    user_concept_id = session.get("user_concept_id")

    # Use existing context from database
    if user_concept_id:
        context = chat_history_service.get_chat_history(user_concept_id, session_id)
    else:
        context = current_app.config.get("CONTEXT", [])

    # REFACTORING_NOTE: Use the new factory to get the correct client and model
    # Get user and org context for per-user/org LLM settings
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()

        # SECURITY: Do NOT trust client-provided user_id - require proper authentication
        # User must be authenticated via:
        # 1. Server-side session (populated during login flow)
        # 2. Validated headers (X-User-Concept-ID with concept validation)
        # If no authenticated user, user_concept_id will be None and RAG tools will be unavailable
        if not user_concept_id:
            current_app.logger.info(
                "[AUTH] No authenticated user for this request. RAG and user-scoped tools will be unavailable. "
                "Client-provided user_id '%s' is ignored for security.",
                request_user_id or "(none)",
            )

        org_concept_id = session.get("organisation_concept_id") if session else None

        # Store user_concept_id in session for history tracking
        if user_concept_id:
            session["user_concept_id"] = user_concept_id
    except Exception:
        user_concept_id = None
        org_concept_id = None

    progress_scope_key = _get_tool_progress_scope_key()
    show_tool_use_progress = False
    try:
        show_tool_use_progress = bool(get_show_tool_use_during_thinking())
    except Exception:
        show_tool_use_progress = False

    if show_tool_use_progress:
        try:
            max_calls = int(get_internal_mcp_max_tool_invocations())
        except Exception:
            max_calls = 0
        try:
            batch_cap = int(get_internal_mcp_tool_batch_cap())
        except Exception:
            batch_cap = 4
        _set_tool_progress(
            progress_scope_key,
            request_id,
            {
                "status": "thinking",
                "request_id": request_id,
                "tool": None,
                "batch_size": None,
                "tool_calls_done": 0,
                "tool_calls_cap": max_calls,
                "tool_calls_remaining": max(0, max_calls),
                "tool_batch_cap": batch_cap,
            },
        )

    try:
        llm_client = get_llm_client(
            user_concept_id=user_concept_id, org_concept_id=org_concept_id
        )
        model_name = get_active_model_name()
    except Exception as e:
        return jsonify({"error": f"Could not get LLM client: {e}"}), 500

    if not prompt_text:
        return jsonify({"error": "No prompt provided."}), 400

    try:
        # Build system message with user and organization context from request
        system_message_parts = []

        # ---------------------------------------------------------
        # JVNAUTOSCI-797: user-specific system prompt from Vontology
        # ---------------------------------------------------------
        auxiliary_system_prompt = None
        narration_prompt_text = None
        narration_prompt_fragments = []
        screen_prompt_text = None
        screen_prompt_fragments = []
        user_prompt_debug = {
            "effective_user_concept_id": user_concept_id,
            "loaded": False,
            "chars": 0,
            "prompt_concept_ids": [],
            "behaviour_prompt_concept_ids": [],
            "narration_prompt_concept_ids": [],
            "screen_prompt_concept_ids": [],
        }
        if user_concept_id:
            try:
                from ...services.chat_auxiliary_prompt_service import (
                    get_user_specific_prompt_fragments,
                )

                behaviour_prompt_fragments = get_user_specific_prompt_fragments(
                    user_concept_id,
                    prompt_types=(
                        "#V#von_chat_behaviour_prompt",
                        "#V#von_chat_behavior_prompt",
                        "#V#von_llm_prompt",
                    ),
                )
                narration_prompt_fragments = get_user_specific_prompt_fragments(
                    user_concept_id,
                    prompt_types=("#V#von_chat_narration_prompt",),
                )
                screen_prompt_fragments = get_user_specific_prompt_fragments(
                    user_concept_id,
                    prompt_types=("#V#von_chat_screen_content_prompt",),
                )

                user_prompt_debug["behaviour_prompt_concept_ids"] = [
                    frag.get("concept_id")
                    for frag in behaviour_prompt_fragments
                    if isinstance(frag, dict)
                    and isinstance(frag.get("concept_id"), str)
                ]
                user_prompt_debug["narration_prompt_concept_ids"] = [
                    frag.get("concept_id")
                    for frag in narration_prompt_fragments
                    if isinstance(frag, dict)
                    and isinstance(frag.get("concept_id"), str)
                ]
                user_prompt_debug["screen_prompt_concept_ids"] = [
                    frag.get("concept_id")
                    for frag in screen_prompt_fragments
                    if isinstance(frag, dict)
                    and isinstance(frag.get("concept_id"), str)
                ]
                # Backwards-compatible field name used by the UI/debug tools.
                user_prompt_debug["prompt_concept_ids"] = list(
                    user_prompt_debug["behaviour_prompt_concept_ids"]
                )

                prompt_texts = []
                for frag in behaviour_prompt_fragments:
                    if not isinstance(frag, dict):
                        continue
                    content = frag.get("content")
                    if not isinstance(content, str):
                        continue
                    if not content.strip():
                        continue
                    prompt_texts.append(content)
                auxiliary_system_prompt = "\n\n".join(
                    text.strip() for text in prompt_texts if text and text.strip()
                )
                auxiliary_system_prompt = (
                    auxiliary_system_prompt.strip() if auxiliary_system_prompt else None
                )

                narration_texts = []
                for frag in narration_prompt_fragments:
                    if not isinstance(frag, dict):
                        continue
                    content = frag.get("content")
                    if not isinstance(content, str):
                        continue
                    if not content.strip():
                        continue
                    narration_texts.append(content)
                narration_prompt_text = "\n\n".join(
                    text.strip() for text in narration_texts if text and text.strip()
                )
                narration_prompt_text = (
                    narration_prompt_text.strip() if narration_prompt_text else None
                )

                screen_texts = []
                for frag in screen_prompt_fragments:
                    if not isinstance(frag, dict):
                        continue
                    content = frag.get("content")
                    if not isinstance(content, str):
                        continue
                    if not content.strip():
                        continue
                    screen_texts.append(content)
                screen_prompt_text = "\n\n".join(
                    text.strip() for text in screen_texts if text and text.strip()
                )
                screen_prompt_text = (
                    screen_prompt_text.strip() if screen_prompt_text else None
                )

                if auxiliary_system_prompt:
                    user_prompt_debug["loaded"] = True
                    user_prompt_debug["chars"] = len(auxiliary_system_prompt)
                if auxiliary_system_prompt:
                    current_app.logger.info(
                        "[CHAT_PROMPT] Loaded %d chars of user-specific prompt for %s",
                        len(auxiliary_system_prompt),
                        user_concept_id,
                    )
            except Exception as e:
                current_app.logger.warning(
                    "[CHAT_PROMPT] Failed loading user-specific prompt for %s: %s",
                    user_concept_id,
                    e,
                )
                user_prompt_debug["error"] = str(e)

        deterministic_introspection_enabled = _deterministic_introspection_enabled()

        if deterministic_introspection_enabled and user_concept_id:
            if _is_prompt_introspection_question(prompt_text):
                return _maybe_handle_prompt_introspection_fastpath(
                    prompt_text=prompt_text,
                    user_concept_id=user_concept_id,
                    session_id=session_id,
                    auxiliary_system_prompt=auxiliary_system_prompt,
                    user_prompt_debug=user_prompt_debug,
                    context=context,
                    interaction_timestamp_utc=interaction_timestamp_utc,
                    model_name=model_name or "unknown",
                    request_start_perf=request_start_perf,
                )

            if _is_rag_status_question(prompt_text):
                return _maybe_handle_rag_status_fastpath(
                    prompt_text=prompt_text,
                    user_concept_id=user_concept_id,
                    session_id=session_id,
                    context=context,
                    interaction_timestamp_utc=interaction_timestamp_utc,
                    model_name=model_name or "unknown",
                    request_start_perf=request_start_perf,
                    user_prompt_debug=user_prompt_debug,
                )

        if deterministic_introspection_enabled and _is_tool_introspection_question(
            prompt_text
        ):
            return _maybe_handle_tool_inventory_fastpath(
                prompt_text=prompt_text,
                user_concept_id=user_concept_id,
                session_id=session_id,
                context=context,
                interaction_timestamp_utc=interaction_timestamp_utc,
                model_name=model_name or "unknown",
                request_start_perf=request_start_perf,
                user_prompt_debug=user_prompt_debug,
            )

        # Try to get user name from concept if user_id provided
        if request_user_id:
            try:
                from ...services.concept_service import get_concept_by_concept_id

                user_concept = get_concept_by_concept_id(request_user_id)
                if user_concept:
                    user_name = user_concept.get("name") or request_user_id
                    system_message_parts.append(
                        f"Current user: {user_name} ({request_user_id})"
                    )
                    current_app.logger.info(
                        f"User context: {user_name} ({request_user_id})"
                    )
                else:
                    system_message_parts.append(f"Current user ID: {request_user_id}")
            except Exception as e:
                current_app.logger.warning(
                    f"Could not fetch user concept {request_user_id}: {e}"
                )
                system_message_parts.append(f"Current user ID: {request_user_id}")

        # Try to get organization name from concept if org_id provided
        if request_org_id:
            try:
                from ...services.concept_service import get_concept_by_concept_id

                org_concept = get_concept_by_concept_id(request_org_id)
                if org_concept:
                    org_name = org_concept.get("name") or request_org_id
                    system_message_parts.append(
                        f"Organization: {org_name} ({request_org_id})"
                    )
                    current_app.logger.info(
                        f"Organization context: {org_name} ({request_org_id})"
                    )
                else:
                    system_message_parts.append(f"Organization ID: {request_org_id}")
            except Exception as e:
                current_app.logger.warning(
                    f"Could not fetch org concept {request_org_id}: {e}"
                )
                system_message_parts.append(f"Organization ID: {request_org_id}")

        # Add language preference if provided
        if request_language and request_language != "en-NZ":
            system_message_parts.append(f"Language preference: {request_language}")

        # Create enhanced context with system message if we have user/org info
        enhanced_context = context.copy()
        if system_message_parts:
            # Create system message
            system_message = "You are Von, an AI assistant. " + " | ".join(
                system_message_parts
            )

            # Insert system message at the beginning if not already present
            if not enhanced_context or enhanced_context[0].get("role") != "system":
                enhanced_context.insert(
                    0, {"role": "system", "content": system_message}
                )
            else:
                # Update existing system message to include user/org context
                existing_system = enhanced_context[0]["content"]
                if not any(part in existing_system for part in system_message_parts):
                    enhanced_context[0][
                        "content"
                    ] = f"{existing_system} | {' | '.join(system_message_parts)}"

        # Ensure user-specific system prompt is included even when the orchestrator
        # is disabled/unavailable.
        if auxiliary_system_prompt:
            user_prompt_message = {
                "role": "system",
                "content": "USER-SPECIFIC SYSTEM PROMPT (from Vontology):\n"
                + auxiliary_system_prompt,
            }
            if not enhanced_context or enhanced_context[0].get("role") != "system":
                enhanced_context.insert(0, user_prompt_message)
            else:
                enhanced_context.insert(1, user_prompt_message)

        # ---------------------------------------------------------
        # JVNAUTOSCI-894: Presenter-mode response protocol
        # ---------------------------------------------------------
        if presenter_mode_requested:
            # Include a small client-reported timing hint for narration generation.
            # (Non-authoritative; used only for guidance.)
            timing_hint = None
            try:
                from ...services.client_capabilities_service import (
                    get_client_capabilities_snapshot,
                )

                snapshot = get_client_capabilities_snapshot()
                speech = (
                    snapshot.get("speech_synthesis")
                    if isinstance(snapshot, dict)
                    else None
                )
                speech = speech if isinstance(speech, dict) else {}
                raw_settings = speech.get("settings")
                settings = raw_settings if isinstance(raw_settings, dict) else {}

                preferred = settings.get("preferred_speaking_seconds")
                maximum = settings.get("max_speaking_seconds")

                try:
                    preferred_int = int(preferred) if preferred is not None else None
                except Exception:
                    preferred_int = None

                try:
                    maximum_int = int(maximum) if maximum is not None else None
                except Exception:
                    maximum_int = None

                if preferred_int is not None:
                    preferred_int = max(1, min(preferred_int, 600))
                if maximum_int is not None:
                    maximum_int = max(1, min(maximum_int, 600))

                effective_preferred = preferred_int
                if preferred_int is not None and maximum_int is not None:
                    effective_preferred = min(preferred_int, maximum_int)

                if effective_preferred is not None or maximum_int is not None:
                    timing_hint = (
                        "Speech timing hint (client-reported, non-authoritative): "
                        f"preferred_speaking_seconds={effective_preferred!r}, "
                        f"max_speaking_seconds={maximum_int!r}. "
                        "Aim for about preferred_speaking_seconds seconds and do not exceed max_speaking_seconds."
                    )
            except Exception:
                timing_hint = None

            presenter_protocol_message = {
                "role": "system",
                "content": (
                    "PRESENTER MODE PROTOCOL:\n"
                    "- Output EXACTLY TWO tagged blocks and nothing else:\n"
                    "  <spoken>...brief talk track...</spoken>\n"
                    "  <screen>...full on-screen content...</screen>\n"
                    "- <spoken> is what will be read aloud (TTS). Keep it short (1–4 sentences), conversational, and focused on the user's intent and what you did / what to do next. Do not read long lists, code blocks, or raw markdown.\n"
                    "- <screen> is what will be shown. It may include structured markdown, code blocks, and full details.\n"
                    "- Do not include <spoken>/<screen> tags inside code blocks.\n"
                    "- Use New Zealand English spelling."
                    + ("\n\n" + timing_hint if timing_hint else "")
                    + (
                        "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
                        "(Applies ONLY to the <spoken> block; do not apply it to <screen>.)\n"
                        + narration_prompt_text
                        if narration_prompt_text
                        else ""
                    )
                    + (
                        "\n\nVON CHAT SCREEN CONTENT PROMPT (from Vontology):\n"
                        "(Applies ONLY to the <screen> block; it must not override tool-grounded facts.)\n"
                        + screen_prompt_text
                        if screen_prompt_text
                        else ""
                    )
                ),
            }
            enhanced_context.insert(0, presenter_protocol_message)

        # Log the enhanced context being sent to the model for debugging
        current_app.logger.info(
            f"Enhanced context being sent to model: {len(enhanced_context)} messages"
        )
        for i, msg in enumerate(enhanced_context):
            current_app.logger.info(
                f"Message {i}: role={msg.get('role')}, content_preview={msg.get('content', '')[:100]}..."
            )

        orchestrator = current_app.config.get("INTERNAL_MCP_ORCHESTRATOR")
        gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
        tool_messages: list[dict[str, str]] = []

        # Derive user namespace for MCP tool isolation (JVNAUTOSCI-760)
        user_namespace = None
        namespace_source = "missing"
        namespace_report: dict[str, object] = {
            "authenticated": bool(user_concept_id),
            "user_concept_id": user_concept_id,
            "session_namespace": session.get("namespace"),
            "namespace": None,
            "namespace_source": None,
        }
        if user_concept_id:
            session_namespace = session.get("namespace")
            if (
                isinstance(session_namespace, str)
                and session_namespace.strip()
                and session_namespace.startswith("#V#")
            ):
                user_namespace = session_namespace.strip()
                namespace_source = "session.namespace"
                current_app.logger.info(
                    "[NAMESPACE] Using session namespace=%s for user_concept_id=%s",
                    user_namespace,
                    user_concept_id,
                )
            else:
                # Convert concept ID to namespace format (#V#michael_witbrock)
                # Handle both full concept ID and person ID formats
                if user_concept_id.startswith("#V#"):
                    user_namespace = user_concept_id
                    namespace_source = "user_concept_id"
                else:
                    # Normalize to namespace format
                    user_id_normalized = (
                        user_concept_id.lower()
                        .replace(" ", "_")
                        .replace("#v#", "")
                        .replace("#", "")
                    )
                    user_namespace = f"#V#{user_id_normalized}"
                    namespace_source = "derived_from_user_concept_id"
                current_app.logger.info(
                    "[NAMESPACE] Derived user_namespace=%s from user_concept_id=%s",
                    user_namespace,
                    user_concept_id,
                )
        else:
            current_app.logger.warning(
                "[NAMESPACE] No user_concept_id - user_namespace=None (RAG unavailable)"
            )

        namespace_report["namespace"] = user_namespace
        namespace_report["namespace_source"] = namespace_source

        rag_trace: dict[str, object] = {
            "authenticated": bool(user_concept_id),
            "namespace": user_namespace,
            "namespace_source": namespace_source,
            "retrieval_attempted": False,
            "retrieval_attempt_reason": (
                None if user_concept_id else "not_authenticated"
            ),
            "tools_invoked": [],
            "tool_results_included_in_prompt": False,
        }

        # ---------------------------------------------------------
        # Tool-backed RAG counts (avoid KA vs chat-history confusion)
        # ---------------------------------------------------------
        # We bypass the LLM for simple factual questions about indexed sessions,
        # because the assistant can otherwise confuse:
        # - interaction sessions (KA sessions) vs
        # - chat history sessions.
        def _is_rag_counts_question(text: str) -> bool:
            lowered = (text or "").strip().lower()
            if not lowered:
                return False

            # Keep this intentionally narrow to avoid hijacking normal chat.
            count_triggers = (
                "how many",
                "count",
                "number of",
            )
            domain_triggers = (
                "indexed",
                "rag",
            )
            chat_triggers = (
                "chat session",
                "chat sessions",
                "chat history",
            )
            ka_triggers = (
                "ka",
                "interaction session",
                "interaction sessions",
            )

            has_count = any(t in lowered for t in count_triggers)
            has_domain = any(t in lowered for t in domain_triggers)
            refers_chat = any(t in lowered for t in chat_triggers)
            refers_ka = any(t in lowered for t in ka_triggers)

            # Examples:
            # - "How many indexed chat sessions can you see?"
            # - "How many chat history sessions are indexed?"
            # - "How many KA sessions are indexed?"
            return has_count and has_domain and (refers_chat or refers_ka)

        if (
            user_concept_id
            and user_namespace
            and gateway is not None
            and getattr(gateway, "enabled", False)
            and _is_rag_counts_question(prompt_text)
        ):
            import json as _json

            user_id_for_history: str = user_concept_id

            tool_invocations = []
            try:
                tool_result = gateway.invoke(
                    "rag_get_status",
                    {"namespace": user_namespace, "detail": 1},
                )
                payload = tool_result.payload
                duration_ms = getattr(tool_result, "duration_ms", None)

                tool_payload = _json.dumps(
                    {
                        "tool": "rag_get_status",
                        "status": "ok",
                        "duration_ms": duration_ms,
                        "payload": payload,
                    },
                    default=str,
                )
                tool_messages = [{"role": "tool", "content": tool_payload}]
                tool_invocations = [
                    {
                        "tool": "rag_get_status",
                        "payload": {"namespace": user_namespace, "detail": 1},
                        "duration_ms": duration_ms,
                        "direct_user_call": False,
                    }
                ]

                # Summarise counts in a way that makes the KA vs chat distinction explicit.
                rs = payload if isinstance(payload, dict) else {}
                ka_indexed = rs.get("indexed")
                ch_sessions = rs.get("chat_history_sessions_in_namespace")
                if ch_sessions is None:
                    ch_sessions = rs.get("chat_history_sessions")

                ch_details = rs.get("chat_history_session_details")
                fully_indexed = None
                any_indexed = None
                if isinstance(ch_details, list) and ch_details:

                    def _as_int(value):
                        try:
                            return int(value)
                        except Exception:
                            return 0

                    fully_indexed = 0
                    any_indexed = 0
                    for row in ch_details:
                        if not isinstance(row, dict):
                            continue
                        missing = _as_int(row.get("messages_missing_index"))
                        ok = _as_int(row.get("rag_indexed_success"))
                        fail = _as_int(row.get("rag_indexed_failed"))
                        indexed_total = row.get("messages_indexed_total")
                        indexed_total_int = (
                            _as_int(indexed_total)
                            if indexed_total is not None
                            else (ok + fail)
                        )

                        if indexed_total_int > 0:
                            any_indexed += 1
                        if missing <= 0:
                            fully_indexed += 1

                parts = []
                parts.append(
                    f"Chat history sessions (in namespace): {ch_sessions if ch_sessions is not None else '—'}"
                )
                if any_indexed is not None:
                    parts.append(
                        f"Chat history sessions with any indexed messages: {any_indexed}"
                    )
                if fully_indexed is not None:
                    parts.append(
                        f"Chat history sessions fully indexed (missing=0): {fully_indexed}"
                    )
                parts.append(
                    f"KA interaction sessions indexed: {ka_indexed if ka_indexed is not None else '—'}"
                )

                response_text = (
                    "Here are the server-truth counts (RAG status), keeping chat history separate from KA interaction sessions:\n\n"
                    + "\n".join(f"- {p}" for p in parts)
                )
            except Exception as exc:
                response_text = (
                    f"I could not retrieve RAG status via internal tools: {exc}"
                )
                tool_messages = []

            # Persist messages in history/context.
            chat_history_service.add_message_to_history(
                user_id_for_history,
                session_id,
                {"role": "user", "content": prompt_text},
            )
            for tool_msg in _truncate_large_tool_results(
                tool_messages, max_tool_content_chars=5000
            ):
                chat_history_service.add_message_to_history(
                    user_id_for_history, session_id, tool_msg
                )
            chat_history_service.add_message_to_history(
                user_id_for_history,
                session_id,
                {"role": "assistant", "content": response_text},
            )
            current_app.config["CONTEXT"] = _limit_context_size(
                current_app.config["CONTEXT"], max_messages=20
            )

            context_stats = _calculate_context_stats(context)
            current_context_stats = _calculate_context_stats(
                current_app.config["CONTEXT"]
            )
            tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None

            llm_debug_info = {
                "interaction_timestamp_utc": interaction_timestamp_utc,
                "model": model_name,
                "llm_interaction": {
                    "requested_model": model_name,
                    "orchestrator_used": False,
                    "duration_ms": None,
                    "usage": None,
                    "calls": [],
                    "server_elapsed_ms": (time.perf_counter() - request_start_perf)
                    * 1000.0,
                },
                "messages": (
                    [{"role": "user", "content": prompt_text}] + tool_messages
                ),
                "response": response_text,
                "user_prompt": user_prompt_debug,
                "context_stats": {
                    "sent_to_llm": context_stats,
                    "stored_context": current_context_stats,
                },
                "tool_stats": tool_stats,
                "tool_invocations": tool_invocations,
                "fastpath": {
                    "name": "rag_counts",
                    "bypassed_llm": True,
                    "used_tool": bool(tool_messages),
                    "enabled": True,
                },
            }
            llm_debug_info["warnings"] = _derive_llm_debug_warnings(llm_debug_info)

            return jsonify(
                {
                    "response": response_text,
                    "fastpath": {
                        "name": "rag_counts",
                        "bypassed_llm": True,
                        "used_tool": bool(tool_messages),
                    },
                    "llm_debug": llm_debug_info,
                }
            )

        # ---------------------------------------------------------
        # Optional debug mode: allow user-issued tool calls
        # ---------------------------------------------------------
        # This is disabled by default because it bypasses the LLM's behavioural
        # guardrails. When enabled, only read-category tools are permitted.
        allow_user_tool_calls = os.getenv(
            "VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0"
        ).lower() in {"1", "true"}
        if allow_user_tool_calls and orchestrator is not None and gateway is not None:
            try:
                direct_request = orchestrator._extract_json_blob(prompt_text)  # type: ignore[attr-defined]
            except ToolCallParsingError:
                direct_request = None

            if direct_request and isinstance(direct_request, dict):
                action = direct_request.get("action")
                tool_name = direct_request.get("tool")
                payload = direct_request.get("payload") or {}

                if (
                    action == "call_tool"
                    and isinstance(tool_name, str)
                    and isinstance(payload, dict)
                ):
                    try:
                        meta = gateway.describe_methods().get(tool_name)  # type: ignore[union-attr]
                        category = (
                            meta.get("category") if isinstance(meta, dict) else None
                        )
                    except Exception:
                        category = None

                    if category != "read":
                        response_text = (
                            "Direct tool calls are restricted to read-only tools. "
                            "Ask Von normally if you need write actions."
                        )
                        tool_invocations = []
                    else:
                        # Direct tool-call mode should not mutate payloads except for
                        # Gmail profile convenience (namespace injection can break
                        # strict schemas like Jira tools).
                        if tool_name.startswith("gmail_"):
                            if request_gmail_profile and not payload.get("profile"):
                                payload["profile"] = request_gmail_profile
                            payload.pop("namespace", None)

                        try:
                            result = gateway.invoke(tool_name, payload)  # type: ignore[union-attr]
                            tool_payload = orchestrator._format_tool_result(tool_name, result.payload, result.duration_ms, "ok")  # type: ignore[attr-defined]
                            response_text = tool_payload
                            tool_messages = [{"role": "tool", "content": tool_payload}]
                            tool_invocations = [
                                {
                                    "tool": tool_name,
                                    "payload": dict(payload),
                                    "direct_user_call": True,
                                }
                            ]
                        except Exception as exc:
                            tool_payload = orchestrator._format_tool_result(tool_name, None, None, "error", str(exc))  # type: ignore[attr-defined]
                            response_text = tool_payload
                            tool_messages = [{"role": "tool", "content": tool_payload}]
                            tool_invocations = [
                                {
                                    "tool": tool_name,
                                    "payload": dict(payload),
                                    "error": str(exc),
                                    "direct_user_call": True,
                                }
                            ]

                    # Skip LLM generation for direct tool calls
                    if user_concept_id:
                        chat_history_service.add_message_to_history(
                            user_concept_id,
                            session_id,
                            {"role": "user", "content": prompt_text},
                        )
                        for tool_msg in _truncate_large_tool_results(
                            tool_messages, max_tool_content_chars=5000
                        ):
                            chat_history_service.add_message_to_history(
                                user_concept_id, session_id, tool_msg
                            )
                        chat_history_service.add_message_to_history(
                            user_concept_id,
                            session_id,
                            {"role": "assistant", "content": response_text},
                        )
                    else:
                        current_app.config["CONTEXT"].append(
                            {"role": "user", "content": prompt_text}
                        )
                        for tool_msg in _truncate_large_tool_results(
                            tool_messages, max_tool_content_chars=5000
                        ):
                            current_app.config["CONTEXT"].append(tool_msg)
                        current_app.config["CONTEXT"].append(
                            {"role": "assistant", "content": response_text}
                        )

                    current_app.config["CONTEXT"] = _limit_context_size(
                        current_app.config["CONTEXT"], max_messages=20
                    )

                    # Return immediately with debug info
                    current_turn_messages = [
                        {"role": "user", "content": prompt_text}
                    ] + tool_messages
                    context_stats = _calculate_context_stats(enhanced_context)
                    current_context_stats = _calculate_context_stats(
                        current_app.config["CONTEXT"]
                    )
                    tool_stats = (
                        _calculate_tool_stats(tool_messages) if tool_messages else None
                    )

                    llm_debug_info = {
                        "interaction_timestamp_utc": interaction_timestamp_utc,
                        "model": model_name,
                        "llm_interaction": {
                            "requested_model": model_name,
                            "orchestrator_used": False,
                            "duration_ms": None,
                            "usage": None,
                            "calls": [],
                            "server_elapsed_ms": (
                                time.perf_counter() - request_start_perf
                            )
                            * 1000.0,
                        },
                        "messages": current_turn_messages,
                        "response": response_text,
                        "user_prompt": user_prompt_debug,
                        "namespace_report": namespace_report,
                        "context_stats": {
                            "sent_to_llm": context_stats,
                            "stored_context": current_context_stats,
                        },
                        "tool_stats": tool_stats,
                        "tool_invocations": tool_invocations,
                        "aux_llm_calls": [],
                    }

                    rag_trace["tools_invoked"] = [
                        inv.get("tool")
                        for inv in tool_invocations
                        if isinstance(inv, dict) and isinstance(inv.get("tool"), str)
                    ]
                    rag_trace["retrieval_attempted"] = False
                    rag_trace["retrieval_attempt_reason"] = "direct_tool_call"

                    return jsonify(
                        {
                            "response": response_text,
                            "llm_debug": llm_debug_info,
                            "rag_trace": rag_trace,
                        }
                    )

        auxiliary_llm_calls: list[dict] = []
        llm_interaction: dict = {
            "requested_model": model_name,
            "orchestrator_used": orchestrator is not None,
            "duration_ms": None,
            "usage": None,
            "calls": [],
        }

        def _infer_provider(model_id: str | None) -> str | None:
            if not isinstance(model_id, str):
                return None
            lowered = model_id.strip().lower()
            if not lowered:
                return None
            if lowered.startswith("openai:"):
                return "openai"
            if lowered.startswith(
                ("gpt-", "o1-", "text-", "davinci", "curie", "babbage", "ada")
            ):
                return "openai"
            if lowered.startswith("gemini"):
                return "gemini"
            if lowered.startswith("ollama:"):
                return "ollama"
            if ":" in lowered and not lowered.startswith("ft:"):
                return "ollama"
            return None

        def _record_stage_llm_call(
            *,
            call_type: str,
            model_name: str | None,
            duration_ms: float | None,
            usage: dict | None = None,
            note: str | None = None,
            stage: str | None = None,
            provider: str | None = None,
        ) -> None:
            payload = {
                "type": call_type,
                "model": model_name,
                "provider": provider or _infer_provider(model_name),
                "duration_ms": duration_ms,
                "usage": usage,
                "workflow": "von_generate",
            }
            if stage:
                payload["stage"] = stage
            if note:
                payload["note"] = note
            llm_interaction["calls"].append(payload)

        if orchestrator is None:
            llm_start_perf = time.perf_counter()
            response_text = llm_client.generate(
                prompt_text, context=enhanced_context, model=model_name
            )
            llm_interaction["duration_ms"] = (
                time.perf_counter() - llm_start_perf
            ) * 1000.0
            llm_interaction["calls"] = [
                {
                    "type": "llm.generate",
                    "model": model_name,
                    "provider": _infer_provider(model_name),
                    "duration_ms": llm_interaction["duration_ms"],
                    "usage": None,
                    "workflow": "von_generate",
                }
            ]
            tool_invocations = []
        else:
            try:
                current_app.logger.info(
                    "[NAMESPACE] Calling orchestrator.run() with user_namespace=%s",
                    user_namespace,
                )
                try:
                    orchestrator.configure_execution_caps(
                        max_tool_invocations=get_internal_mcp_max_tool_invocations(),
                        tool_batch_cap=get_internal_mcp_tool_batch_cap(),
                    )
                except Exception:
                    # Defensive: never fail the request due to settings refresh.
                    pass

                if show_tool_use_progress:

                    def _progress_update(info: dict[str, Any]) -> None:
                        payload = (
                            dict(info)
                            if isinstance(info, dict)
                            else {"status": "unknown"}
                        )
                        payload.setdefault("request_id", request_id)
                        _set_tool_progress(progress_scope_key, request_id, payload)

                    try:
                        orchestrator.set_progress_callback(_progress_update)
                    except Exception:
                        pass

                orchestrator_start_perf = time.perf_counter()
                try:
                    orchestrator_result = orchestrator.run(
                        prompt=prompt_text,
                        context=enhanced_context,
                        llm_client=llm_client,
                        model=model_name,
                        user_namespace=user_namespace,
                        gmail_profile=request_gmail_profile,
                        auxiliary_system_prompt=auxiliary_system_prompt,
                        preferred_language=request_language,
                    )
                finally:
                    if show_tool_use_progress:
                        try:
                            orchestrator.set_progress_callback(None)
                        except Exception:
                            pass
                llm_interaction["duration_ms"] = (
                    time.perf_counter() - orchestrator_start_perf
                ) * 1000.0
                llm_interaction["calls"] = list(
                    getattr(orchestrator_result, "llm_calls", [])
                )
                llm_interaction["usage"] = getattr(
                    orchestrator_result, "llm_usage", None
                )
                llm_interaction["orchestrator_duration_ms"] = getattr(
                    orchestrator_result, "orchestrator_duration_ms", None
                )
                response_text = orchestrator_result.response_text
                tool_messages = [
                    dict(msg) for msg in orchestrator_result.extra_messages
                ]
                tool_invocations = list(orchestrator_result.tool_invocations)
                auxiliary_llm_calls = list(
                    getattr(orchestrator_result, "aux_llm_calls", [])
                )

                invoked_tools = []
                for inv in tool_invocations:
                    if not isinstance(inv, dict):
                        continue
                    name = inv.get("tool") or inv.get("method")
                    if isinstance(name, str) and name:
                        invoked_tools.append(name)

                rag_trace["tools_invoked"] = invoked_tools
                rag_trace["retrieval_attempted"] = (
                    "search_knowledge_base" in invoked_tools
                )
                if rag_trace["retrieval_attempted"]:
                    rag_trace["retrieval_attempt_reason"] = "tool_invoked"
                else:
                    rag_trace["retrieval_attempt_reason"] = (
                        "no_rag_retrieval_tool_invoked"
                        if user_concept_id
                        else "not_authenticated"
                    )
                rag_trace["tool_results_included_in_prompt"] = bool(tool_messages)

                if show_tool_use_progress:
                    _set_tool_progress(
                        progress_scope_key,
                        request_id,
                        {
                            "status": "completed",
                            "request_id": request_id,
                        },
                    )
            except ToolCallParsingError as exc:
                current_app.logger.warning(
                    "[mcp_orchestrator] Invalid tool request payload: %s", exc
                )
                response_text = (
                    "Tool call was not executed due to an MCP serialisation error. "
                    f"({exc})\n\n"
                    "Please try again. If this keeps happening, copy the LLM debug output so we can reproduce it."
                )
                rejected_tool_call = _truncate_debug_payload(
                    getattr(exc, "raw_response", None)
                )
                tool_messages = []
                tool_invocations = [
                    {
                        "tool": "__tool_call_parse_error__",
                        "payload": (
                            {"raw_tool_call": rejected_tool_call}
                            if rejected_tool_call is not None
                            else {}
                        ),
                        "error": str(exc),
                    }
                ]

                if show_tool_use_progress:
                    _set_tool_progress(
                        progress_scope_key,
                        request_id,
                        {
                            "status": "error",
                            "request_id": request_id,
                            "error": str(exc),
                        },
                    )

        presenter_channels = _extract_presenter_channels(response_text)
        presenter_channels_missing = (
            not isinstance(presenter_channels, dict) or not presenter_channels
        )

        screen_backfill_second_pass_attempted = False
        screen_backfill_second_pass_reason = None

        if presenter_mode_requested:
            has_tool_messages = bool(tool_messages)
            screen_tag_present = _presenter_tag_present(response_text, "screen")
            screen_text = None
            spoken_text = None
            if isinstance(presenter_channels, dict):
                screen_value = presenter_channels.get("screen")
                if isinstance(screen_value, str) and screen_value.strip():
                    screen_text = screen_value.strip()
                spoken_value = presenter_channels.get("spoken")
                if isinstance(spoken_value, str) and spoken_value.strip():
                    spoken_text = spoken_value.strip()

            def _screen_looks_like_tool_dump(value: str) -> bool:
                lowered = (value or "").strip().lower()
                if not lowered:
                    return True
                if lowered.startswith("tool results:"):
                    return True
                if "```json" in lowered or '"payload"' in lowered:
                    return True
                return False

            def _screen_too_similar_to_spoken(
                screen_value: str | None, spoken_value: str | None
            ) -> bool:
                if not screen_value or not spoken_value:
                    return False
                a = screen_value.strip()
                b = spoken_value.strip()
                if not a or not b:
                    return False
                # Heuristic: if the screen is exactly the talk track, it usually means the
                # model emitted only <spoken> and we defaulted screen=spoken.
                return a == b

            needs_screen_backfill = (
                (not screen_tag_present)
                or (not isinstance(screen_text, str) or not screen_text.strip())
                or (
                    isinstance(screen_text, str)
                    and _screen_looks_like_tool_dump(screen_text)
                )
                or _screen_too_similar_to_spoken(screen_text, spoken_text)
            )

            def _strip_presenter_tags(text: str) -> str | None:
                if not isinstance(text, str) or not text:
                    return None
                import re

                cleaned = re.sub(r"</?spoken>", "", text, flags=re.IGNORECASE)
                cleaned = re.sub(r"</?screen>", "", cleaned, flags=re.IGNORECASE)
                cleaned = cleaned.strip()
                return cleaned or None

            if needs_screen_backfill:
                screen_backfill_second_pass_attempted = True
                if show_tool_use_progress:
                    _set_tool_progress(
                        progress_scope_key,
                        request_id,
                        {
                            "status": "workflow",
                            "request_id": request_id,
                            "workflow_task": "screen_backfill",
                        },
                    )
                if not screen_tag_present or not screen_text:
                    screen_backfill_second_pass_reason = "missing_screen"
                elif _screen_looks_like_tool_dump(screen_text or ""):
                    screen_backfill_second_pass_reason = "tool_payload_screen"
                else:
                    screen_backfill_second_pass_reason = "screen_matches_spoken"

                tool_blob = (
                    _build_tool_messages_prompt_blob(tool_messages)
                    if has_tool_messages
                    else ""
                )

                def _tool_messages_include_description_write(
                    messages: list[dict],
                ) -> bool:
                    """Best-effort detection of a description write tool action.

                    We keep this conservative: if we cannot identify a description
                    write confidently, return False.
                    """

                    import json

                    for message in messages:
                        if message.get("role") != "tool":
                            continue
                        content = message.get("content")
                        if not isinstance(content, str) or not content.strip():
                            continue

                        try:
                            parsed = json.loads(content)
                        except Exception:
                            continue

                        if not isinstance(parsed, dict):
                            continue

                        tool_name = (
                            parsed.get("tool")
                            or parsed.get("method")
                            or parsed.get("name")
                            or ""
                        )
                        tool_name = tool_name if isinstance(tool_name, str) else ""
                        tool_name_lower = tool_name.lower()

                        payload = parsed.get("payload")
                        payload = payload if isinstance(payload, dict) else {}

                        if "description" in tool_name_lower:
                            return True

                        predicate = payload.get("predicate")
                        if isinstance(predicate, str) and predicate.strip() in {
                            "hasDescription",
                            "has_description",
                            "#V#hasDescription",
                        }:
                            return True

                        if (
                            isinstance(payload.get("description"), str)
                            and payload.get("description").strip()
                        ):
                            return True

                    return False

                description_write_seen = (
                    _tool_messages_include_description_write(tool_messages)
                    if has_tool_messages
                    else False
                )

                screen_candidate = None
                screen_backfill_source = None
                response_candidate = _strip_presenter_tags(response_text)
                if response_candidate and not _screen_looks_like_tool_dump(
                    response_candidate
                ):
                    screen_candidate = response_candidate
                    screen_backfill_source = "response_text"
                allow_llm_screen_synthesis = os.getenv(
                    "VON_PRESENTER_SCREEN_BACKFILL_USE_LLM", "1"
                ).lower() in {"1", "true"}

                if screen_candidate is None and allow_llm_screen_synthesis:
                    try:
                        if has_tool_messages:
                            synthesis_system = (
                                "You are Von. Create the on-screen response for the chat UI. "
                                "Return ONLY one block: <screen>...</screen>. "
                                "Do not include <spoken>. Do not include JSON. "
                                "Use New Zealand English spelling. "
                                "CRITICAL: Only state facts that are explicitly present in the tool results summary. "
                                "Do not infer, guess, or add any claims beyond tool outputs."
                            )
                            synthesis_user = (
                                "User request:\n"
                                f"{prompt_text}\n\n"
                                "Model response (may be incomplete; NOT authoritative for tool-backed changes):\n"
                                f"{response_text}\n\n"
                                + (
                                    "VON CHAT SCREEN CONTENT PROMPT (from Vontology):\n"
                                    "(Applies ONLY to <screen> formatting; it must not override tool-grounded facts.)\n"
                                    + str(screen_prompt_text).strip()
                                    + "\n\n"
                                    if isinstance(screen_prompt_text, str)
                                    and screen_prompt_text.strip()
                                    else ""
                                )
                                + "Tool results summary (authoritative):\n"
                                f"{tool_blob}\n"
                                "\nNon-negotiable rule:\n"
                                "- If the tool results summary does not explicitly show a description update, you MUST NOT claim the description was added/updated. "
                                "  You may say it is still empty/vacuous or that no tool updated it.\n"
                                "- You MUST include a short section titled 'Writes ledger (authoritative)' that reflects the tool writes ledger without contradiction.\n"
                            )
                        else:
                            synthesis_system = (
                                "You are Von. Create the on-screen response for the chat UI. "
                                "Return ONLY one block: <screen>...</screen>. "
                                "Do not include <spoken>. Do not include JSON. "
                                "Use New Zealand English spelling. "
                                "Use only the user request and the model response as sources. "
                                "Do not invent facts beyond what is stated there."
                            )
                            synthesis_user = (
                                "User request:\n"
                                f"{prompt_text}\n\n"
                                "Model response (may be incomplete; use it as content to display):\n"
                                f"{response_text}\n\n"
                                + (
                                    "VON CHAT SCREEN CONTENT PROMPT (from Vontology):\n"
                                    "(Applies ONLY to <screen> formatting.)\n"
                                    + str(screen_prompt_text).strip()
                                    + "\n\n"
                                    if isinstance(screen_prompt_text, str)
                                    and screen_prompt_text.strip()
                                    else ""
                                )
                            )

                        synthesis_response = None
                        if orchestrator is not None and hasattr(
                            orchestrator, "_run_llm_with_fallbacks"
                        ):
                            try:
                                policy_state, _ = (
                                    orchestrator._load_workflow_model_policy(
                                        request_language
                                    )
                                )
                                synthesis_response, screen_model_used, _ = (
                                    orchestrator._run_llm_with_fallbacks(
                                        stage="screen_backfill",
                                        prompt="Generate <screen> display content",
                                        context=[
                                            {
                                                "role": "system",
                                                "content": synthesis_system,
                                            },
                                            {"role": "user", "content": synthesis_user},
                                        ],
                                        default_client=llm_client,
                                        default_model=model_name,
                                        policy_state=policy_state,
                                        user_concept_id=user_concept_id,
                                        org_concept_id=org_concept_id,
                                        llm_calls_log=llm_interaction["calls"],
                                        aux_log=auxiliary_llm_calls,
                                        record_llm_call=_record_stage_llm_call,
                                    )
                                )
                            except Exception:
                                synthesis_response = None
                        if synthesis_response is None:
                            llm_start = time.perf_counter()
                            screen_model_used = model_name
                            synthesis_response = llm_client.generate(
                                prompt="Generate <screen> display content",
                                context=[
                                    {"role": "system", "content": synthesis_system},
                                    {"role": "user", "content": synthesis_user},
                                ],
                                model=screen_model_used,
                            )
                            _record_stage_llm_call(
                                call_type="llm.generate",
                                model_name=screen_model_used,
                                duration_ms=(time.perf_counter() - llm_start) * 1000.0,
                                usage=None,
                                note="Screen backfill synthesis (legacy).",
                                stage="screen_backfill",
                                provider=_infer_provider(screen_model_used),
                            )

                        screen_candidate = _extract_screen_only(str(synthesis_response))
                        if not screen_candidate:
                            raw = str(synthesis_response).strip()
                            if raw:
                                screen_candidate = raw
                        if screen_candidate:
                            screen_backfill_source = "llm_synthesis"
                    except Exception:
                        screen_candidate = None

                # If the LLM tries to claim a description write without evidence,
                # discard it and fall back to the deterministic tool summary.
                if (
                    screen_candidate
                    and not description_write_seen
                    and isinstance(screen_candidate, str)
                ):
                    import re

                    lowered = screen_candidate.lower()
                    description_claim_patterns = (
                        r"\bdescription\s+(?:has\s+been\s+)?(?:added|updated|set|filled)\b",
                        r"\badded\s+(?:a\s+)?description\b",
                        r"\bupdated\s+(?:the\s+)?description\b",
                    )
                    if any(
                        re.search(p, lowered, flags=re.IGNORECASE)
                        for p in description_claim_patterns
                    ):
                        screen_candidate = None

                if not screen_candidate:
                    screen_candidate = (
                        _build_presenter_screen_summary_from_tool_messages(
                            tool_messages
                        )
                    )
                    if screen_candidate:
                        screen_backfill_source = "tool_summary"

                if screen_candidate:
                    base_channels = (
                        dict(presenter_channels)
                        if isinstance(presenter_channels, dict)
                        else {}
                    )
                    base_channels["screen"] = str(screen_candidate).strip()
                    if screen_backfill_source == "response_text":
                        base_channels["format"] = "screen_backfill_from_response_v1"
                    else:
                        base_channels["format"] = "screen_backfill_from_tools_v1"
                    presenter_channels = base_channels

                    auxiliary_llm_calls.append(
                        {
                            "type": "workflow_stage",
                            "stage": "screen_backfill",
                            "source": screen_backfill_source,
                            "reason": screen_backfill_second_pass_reason,
                            "format": base_channels.get("format"),
                        }
                    )

        def _extract_spoken_only(text: str) -> str | None:
            if not isinstance(text, str) or not text:
                return None
            import re

            searchable = _strip_fenced_code_blocks(text)
            m = re.search(
                r"<spoken>\s*(.*?)\s*</spoken>",
                searchable,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if not m:
                return None
            value = m.group(1)
            if not isinstance(value, str):
                return None
            cleaned = value.strip()
            return cleaned or None

        def _coerce_spoken_text(text: object) -> str | None:
            """Best-effort normalisation for narration responses.

            The narration second-pass *should* return <spoken>...</spoken>, but in
            practice models sometimes return plain text. Accept either format.
            """

            if text is None:
                return None

            raw = str(text).strip()
            if not raw:
                return None

            tagged = _extract_spoken_only(raw)
            if tagged:
                return tagged

            import re

            # Strip any accidental tagged blocks and code fences.
            raw = re.sub(r"</?spoken>", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"</?screen>", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
            raw = raw.replace("`", "")
            raw = raw.strip()

            if not raw:
                return None

            # Keep it short for TTS.
            try:
                max_chars = int(os.getenv("VON_NARRATION_SPOKEN_MAX_CHARS", "4000"))
            except ValueError:
                max_chars = 4000
            max_chars = max(200, min(max_chars, 50000))
            if len(raw) > max_chars:
                clipped = raw[:max_chars].rstrip()
                # Prefer clipping at a sentence boundary.
                for sep in (". ", "! ", "? "):
                    cut = clipped.rfind(sep)
                    if cut > 200:
                        clipped = clipped[: cut + 1]
                        break
                raw = clipped.strip()

            return raw or None

        spoken_backfill_second_pass_attempted = False
        spoken_backfill_second_pass_reason = None

        # If presenter mode was requested but the model didn't produce a usable
        # <spoken> channel, generate it in a second pass for reliability.
        #
        # Note: The model may emit only <screen>...</screen>. In that case,
        # we still generate <spoken> using the narration prompt rather than
        # falling back to reading the screen/markdown verbatim.
        needs_spoken_backfill = False
        if presenter_mode_requested:
            if presenter_channels_missing:
                needs_spoken_backfill = True
                if has_tool_messages:
                    spoken_backfill_second_pass_reason = "missing_spoken"
                else:
                    spoken_backfill_second_pass_reason = "missing_presenter_channels"
            elif isinstance(presenter_channels, dict):
                spoken_value = presenter_channels.get("spoken")
                if not isinstance(spoken_value, str) or not spoken_value.strip():
                    needs_spoken_backfill = True
                    spoken_backfill_second_pass_reason = "missing_spoken"
            else:
                needs_spoken_backfill = True
                spoken_backfill_second_pass_reason = "missing_presenter_channels"

        if needs_spoken_backfill:
            try:
                spoken_backfill_second_pass_attempted = True
                if show_tool_use_progress:
                    _set_tool_progress(
                        progress_scope_key,
                        request_id,
                        {
                            "status": "workflow",
                            "request_id": request_id,
                            "workflow_task": "narration_planning",
                        },
                    )
                if isinstance(presenter_channels, dict) and isinstance(
                    presenter_channels.get("screen"), str
                ):
                    screen_text = str(presenter_channels.get("screen") or "").strip()
                else:
                    screen_text = (
                        response_text.strip()
                        if isinstance(response_text, str)
                        else str(response_text)
                    )

                narration_data = {
                    "presenter_mode_requested": presenter_mode_requested,
                    "screen_text": screen_text,
                    "user_prompt": prompt_text,
                    "narration_prompt_text": narration_prompt_text,
                    "narration_prompt_ids": [
                        frag.get("concept_id")
                        for frag in (narration_prompt_fragments or [])
                        if isinstance(frag, dict)
                    ],
                    "presenter_channels": (
                        dict(presenter_channels)
                        if isinstance(presenter_channels, dict)
                        else {}
                    ),
                }

                narration_trace = None
                narration_trace_store = None
                narration_trace_enabled = os.getenv(
                    "VON_WORKFLOWS_TRACE_ENABLED", "0"
                ).lower() in {"1", "true"}
                if narration_trace_enabled:
                    try:
                        narration_trace = WorkflowExecutionTrace(
                            workflow_id=CHAT_NARRATION_WORKFLOW_ID
                        )
                        narration_trace.user_namespace = user_namespace
                        narration_trace_store = insert_workflow_execution_trace
                    except Exception:
                        narration_trace = None
                        narration_trace_store = None
                        narration_trace_enabled = False

                workflow_result = None
                if orchestrator is not None:
                    workflow_result = orchestrator.execute_workflow(
                        CHAT_NARRATION_WORKFLOW_ID,
                        data=narration_data,
                        llm_client=llm_client,
                        model=model_name,
                        user_namespace=user_namespace,
                        auxiliary_system_prompt=auxiliary_system_prompt,
                        trace=narration_trace,
                    )

                if workflow_result is not None:
                    channels = workflow_result.data.get(
                        "presenter_channels", presenter_channels
                    )
                    if isinstance(channels, dict) and channels:
                        presenter_channels = channels
                    if narration_trace_enabled and narration_trace is not None:
                        try:
                            if workflow_result.completed:
                                narration_trace.finish_completed()
                            elif workflow_result.error:
                                narration_trace.finish_failed(workflow_result.error)
                        except Exception:
                            pass
                        if narration_trace_store is not None:
                            try:
                                stored_exec = narration_trace_store(
                                    narration_trace.to_storage_document()
                                )
                                auxiliary_llm_calls.append(
                                    {
                                        "type": "workflow_execution_trace",
                                        "path": "narration",
                                        "workflow_id": CHAT_NARRATION_WORKFLOW_ID,
                                        "execution_id": narration_trace.execution_id,
                                        "stored": bool(stored_exec),
                                        "status": narration_trace.status,
                                    }
                                )
                            except Exception:
                                pass
                else:
                    # Include a small client-reported timing hint for narration generation.
                    # (Non-authoritative; used only for guidance.)
                    timing_hint = None
                    try:
                        from ...services.client_capabilities_service import (
                            get_client_capabilities_snapshot,
                        )

                        snapshot = get_client_capabilities_snapshot()
                        speech = (
                            snapshot.get("speech_synthesis")
                            if isinstance(snapshot, dict)
                            else None
                        )
                        speech = speech if isinstance(speech, dict) else {}
                        raw_settings = speech.get("settings")
                        settings = (
                            raw_settings if isinstance(raw_settings, dict) else {}
                        )

                        preferred = settings.get("preferred_speaking_seconds")
                        maximum = settings.get("max_speaking_seconds")

                        try:
                            preferred_int = (
                                int(preferred) if preferred is not None else None
                            )
                        except Exception:
                            preferred_int = None

                        try:
                            maximum_int = int(maximum) if maximum is not None else None
                        except Exception:
                            maximum_int = None

                        if preferred_int is not None:
                            preferred_int = max(1, min(preferred_int, 600))
                        if maximum_int is not None:
                            maximum_int = max(1, min(maximum_int, 600))

                        effective_preferred = preferred_int
                        if preferred_int is not None and maximum_int is not None:
                            effective_preferred = min(preferred_int, maximum_int)

                        if effective_preferred is not None or maximum_int is not None:
                            timing_hint = (
                                "Speech timing hint (client-reported, non-authoritative): "
                                f"preferred_speaking_seconds={effective_preferred!r}, "
                                f"max_speaking_seconds={maximum_int!r}. "
                                "Aim for about preferred_speaking_seconds seconds and do not exceed max_speaking_seconds."
                            )
                    except Exception:
                        timing_hint = None

                    narration_system = (
                        "You are Von. Produce a short talk track for text-to-speech. "
                        "Return ONLY one block: <spoken>...</spoken>. "
                        "Do not include <screen>. Do not include code blocks. "
                        "Use New Zealand English spelling."
                        + ("\n\n" + timing_hint if timing_hint else "")
                        + (
                            "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
                            + narration_prompt_text
                            if narration_prompt_text
                            else ""
                        )
                    )

                    narration_user = (
                        "User message:\n"
                        f"{prompt_text}\n\n"
                        "On-screen content (do not read verbatim if long; summarise):\n"
                        f"{screen_text}\n"
                    )

                    narration_response = llm_client.generate(
                        prompt="Generate <spoken> talk track",
                        context=[
                            {"role": "system", "content": narration_system},
                            {"role": "user", "content": narration_user},
                        ],
                        model=model_name,
                    )

                    spoken_fallback = _coerce_spoken_text(narration_response)
                    spoken_from_screen_text = False
                    if not spoken_fallback:
                        spoken_fallback = _coerce_spoken_text(screen_text)
                        spoken_from_screen_text = bool(spoken_fallback)

                    if spoken_fallback:
                        base_channels = (
                            dict(presenter_channels)
                            if isinstance(presenter_channels, dict)
                            else {}
                        )
                        base_channels["screen"] = screen_text
                        base_channels["spoken"] = spoken_fallback
                        existing_format = base_channels.get("format")
                        # Preserve formats that describe *how the screen* was produced.
                        # For normal tagged responses, surface that narration was added.
                        if existing_format == "tool_results_fallback_v1":
                            base_channels["format"] = existing_format
                        elif (
                            has_tool_messages
                            and isinstance(existing_format, str)
                            and existing_format.startswith("screen_backfill_")
                        ):
                            base_channels["format"] = existing_format
                        elif (
                            not has_tool_messages
                            and isinstance(existing_format, str)
                            and existing_format.startswith("screen_backfill_")
                        ):
                            base_channels["format"] = "narration_fallback_v1"
                        elif (
                            spoken_backfill_second_pass_reason
                            == "missing_presenter_channels"
                        ):
                            base_channels["format"] = "narration_fallback_v1"
                        elif (
                            spoken_from_screen_text
                            and isinstance(existing_format, str)
                            and existing_format.startswith("screen_backfill_")
                        ):
                            base_channels["format"] = existing_format
                        else:
                            base_channels["format"] = "narration_fallback_v1"
                        presenter_channels = base_channels
            except Exception:
                # Defensive: never fail the request just because narration generation failed.
                presenter_channels = presenter_channels

        if presenter_channels is not None:
            # Screen channel becomes the stored/displayed response.
            # Exception: tool-results fallback is debug-only; do not show it as the main chat bubble.
            presenter_format = presenter_channels.get("format")
            screen_text_value = presenter_channels.get("screen")

            if presenter_format == "tool_results_fallback_v1":
                # Keep the model's user-facing response when available.
                # Only strip <spoken> tags if present, or fall back to spoken when the
                # response is empty.
                extracted = _extract_spoken_only(response_text)
                if extracted:
                    response_text = extracted
                elif isinstance(response_text, str) and response_text.strip():
                    pass
                else:
                    spoken_value = presenter_channels.get("spoken")
                    if isinstance(spoken_value, str) and spoken_value.strip():
                        response_text = spoken_value.strip()
            else:
                if isinstance(screen_text_value, str) and screen_text_value.strip():
                    response_text = screen_text_value.strip()

        if tool_invocations:
            current_app.logger.info(
                "[mcp_orchestrator] Tool invocations: %s", tool_invocations
            )

        # Build LLM debug information FIRST (before saving to history)
        # so we can persist it alongside the assistant message
        # NOTE: Only include the NEW messages for this turn to avoid exponential token growth
        # as the full context would include all previous turns' debug data
        current_turn_messages = [{"role": "user", "content": prompt_text}]
        if tool_messages:
            current_turn_messages.extend(tool_messages)

        # Calculate context statistics for visibility.
        # If the internal orchestrator is enabled, it augments and trims the context
        # before sending it to the LLM, so report stats for the *actual* sent context.
        sent_context_for_stats = enhanced_context
        if orchestrator is not None:
            build_augmented = getattr(orchestrator, "_build_augmented_context", None)
            if callable(build_augmented):
                try:
                    sent_context_for_stats = build_augmented(
                        enhanced_context,
                        user_namespace=user_namespace,
                        auxiliary_system_prompt=auxiliary_system_prompt,
                    )
                except Exception:
                    sent_context_for_stats = enhanced_context

        sent_context_stats_messages: list[dict] = enhanced_context
        if isinstance(sent_context_for_stats, list):
            normalised_messages: list[dict] = []
            for msg in sent_context_for_stats:
                if isinstance(msg, dict):
                    normalised_messages.append(msg)
                    continue
                try:
                    normalised_messages.append(dict(msg))
                except Exception:
                    continue
            if normalised_messages:
                sent_context_stats_messages = normalised_messages

        buttonify_options: list[str] = []
        buttonify_meta: dict[str, Any] | None = None
        buttonify_enabled = get_buttonify_model_enabled()
        buttonify_allowed = (
            not current_app.testing
            and not os.getenv("PYTEST_CURRENT_TEST")
            and (
                orchestrator is None or hasattr(orchestrator, "_run_llm_with_fallbacks")
            )
        )
        if (
            buttonify_enabled
            and buttonify_allowed
            and isinstance(response_text, str)
            and response_text
        ):
            if show_tool_use_progress:
                _set_tool_progress(
                    progress_scope_key,
                    request_id,
                    {
                        "status": "workflow",
                        "request_id": request_id,
                        "workflow_task": "buttonify",
                    },
                )
            buttonify_prompt_template = (
                "You generate quick-reply button options for a chat UI.\n\n"
                "Use the user message and assistant response. Extract up to 4 options that the user could tap next.\n\n"
                "Rules:\n"
                "- Return ONLY a JSON array of strings. No prose, no Markdown.\n"
                "- Each option must be 1-4 words and safe to send verbatim.\n"
                "- Prefer exact wording from the response when explicit (lists, quoted replies, template choices).\n"
                '- If the response presents implicit alternatives (e.g. "Would you like to continue or stop?"), convert them into concise options (e.g. ["Continue", "Stop"]).\n'
                '- If the response is a yes/no question without explicit options, return ["Yes", "No"].\n'
                "- If there are no clear options or it is open-ended, return [].\n"
                "- Do not invent options beyond what is stated or clearly implied.\n"
                "- Avoid punctuation, emojis, or more than 4 words.\n\n"
                "User message:\n{user_message}\n\nAssistant response:\n{assistant_response}"
            )

            prompt_service = PromptTemplateService()
            rendered_buttonify_prompt = None
            try:
                rendered_buttonify_prompt = prompt_service.render_prompt(
                    _BUTTONIFY_PROMPT_IDS,
                    variables={
                        "user_message": prompt_text,
                        "assistant_response": response_text,
                    },
                    fallback=buttonify_prompt_template,
                )
            except Exception:
                rendered_buttonify_prompt = None

            if rendered_buttonify_prompt:
                buttonify_prompt = rendered_buttonify_prompt.text
                buttonify_prompt_id = rendered_buttonify_prompt.prompt_id
                buttonify_prompt_truncated = rendered_buttonify_prompt.truncated
            else:
                buttonify_prompt = buttonify_prompt_template.format(
                    user_message=prompt_text,
                    assistant_response=response_text,
                )
                buttonify_prompt_id = None
                buttonify_prompt_truncated = False

            buttonify_context: list[dict[str, Any]] = []
            buttonify_response = None
            buttonify_model_used = model_name
            if orchestrator is not None and hasattr(
                orchestrator, "_run_llm_with_fallbacks"
            ):
                try:
                    policy_state, _ = orchestrator._load_workflow_model_policy(
                        request_language
                    )
                    buttonify_response, buttonify_model_used, _ = (
                        orchestrator._run_llm_with_fallbacks(
                            stage="buttonify",
                            prompt=buttonify_prompt,
                            context=buttonify_context,
                            default_client=llm_client,
                            default_model=model_name,
                            policy_state=policy_state,
                            user_concept_id=user_concept_id,
                            org_concept_id=org_concept_id,
                            llm_calls_log=llm_interaction["calls"],
                            aux_log=auxiliary_llm_calls,
                            record_llm_call=_record_stage_llm_call,
                        )
                    )
                except Exception:
                    buttonify_response = None
            if buttonify_response is None:
                llm_start = time.perf_counter()
                buttonify_response = llm_client.generate(
                    prompt=buttonify_prompt,
                    context=buttonify_context,
                    model=buttonify_model_used,
                )
                _record_stage_llm_call(
                    call_type="llm.generate",
                    model_name=buttonify_model_used,
                    duration_ms=(time.perf_counter() - llm_start) * 1000.0,
                    usage=None,
                    note="Buttonify quick-reply extraction (legacy).",
                    stage="buttonify",
                )
            try:
                import json as _json

                parsed = _json.loads(str(buttonify_response))
                if isinstance(parsed, list):
                    for item in parsed:
                        if not isinstance(item, str):
                            continue
                        cleaned = item.strip()
                        if not cleaned:
                            continue
                        if len(cleaned.split()) > 4:
                            continue
                        if len(cleaned) > 60:
                            continue
                        buttonify_options.append(cleaned)
            except Exception:
                buttonify_options = []

            buttonify_source = "llm" if buttonify_options else "none"
            if not buttonify_options:
                heuristic_options = _extract_buttonify_options_heuristic(response_text)
                if heuristic_options:
                    buttonify_options = heuristic_options
                    buttonify_source = "heuristic"

            buttonify_meta = {
                "enabled": True,
                "model": buttonify_model_used,
                "options": buttonify_options,
                "source": buttonify_source,
                "prompt_id": buttonify_prompt_id,
                "prompt_truncated": buttonify_prompt_truncated,
            }

        context_stats = _calculate_context_stats(sent_context_stats_messages)

        # stored_context should reflect the persisted user/session history when authenticated,
        # not the unauthenticated in-memory CONTEXT list.
        if user_concept_id:
            try:
                persisted_history = chat_history_service.get_chat_history(
                    user_concept_id, session_id
                )
            except Exception:
                persisted_history = []
            current_context_stats = _calculate_context_stats(persisted_history)
        else:
            current_context_stats = _calculate_context_stats(
                current_app.config["CONTEXT"]
            )

        # Calculate tool statistics if tools were used
        tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None

        # Capture a compact summary of the tool catalogue that the agent was shown.
        # This improves trace transparency without storing the full system prompt.
        tool_catalogue_summary = None
        if gateway is not None:
            try:
                methods_snapshot = gateway.describe_methods()
                if isinstance(methods_snapshot, dict):
                    method_names = sorted(
                        [
                            name
                            for name in methods_snapshot.keys()
                            if isinstance(name, str)
                        ]
                    )
                    try:
                        import hashlib
                        import json

                        digest = hashlib.sha256(
                            json.dumps(
                                method_names,
                                separators=(",", ":"),
                                ensure_ascii=True,
                            ).encode("utf-8")
                        ).hexdigest()
                    except Exception:
                        digest = None

                    sample_cap = 25
                    tool_catalogue_summary = {
                        "method_count": len(method_names),
                        "sha256": digest,
                        "sample": method_names[:sample_cap],
                        "sample_truncated": len(method_names) > sample_cap,
                    }
            except Exception:
                tool_catalogue_summary = None

        try:
            applied_max_tool_invocations = int(get_internal_mcp_max_tool_invocations())
        except Exception:
            applied_max_tool_invocations = None

        try:
            applied_tool_batch_cap = int(get_internal_mcp_tool_batch_cap())
        except Exception:
            applied_tool_batch_cap = None

        llm_debug_info = {
            "interaction_timestamp_utc": interaction_timestamp_utc,
            "request_id": request_id,
            "model": model_name,
            "llm_interaction": {
                **llm_interaction,
                "server_elapsed_ms": (time.perf_counter() - request_start_perf)
                * 1000.0,
            },
            "messages": current_turn_messages,
            "response": response_text,
            "presenter_channels": presenter_channels,
            "screen_backfill_second_pass_attempted": screen_backfill_second_pass_attempted,
            "screen_backfill_second_pass_reason": screen_backfill_second_pass_reason,
            "spoken_backfill_second_pass_attempted": spoken_backfill_second_pass_attempted,
            "spoken_backfill_second_pass_reason": spoken_backfill_second_pass_reason,
            "user_prompt": user_prompt_debug,
            "namespace_report": namespace_report,
            "internal_mcp": {
                "gateway_present": gateway is not None,
                "gateway_enabled": (
                    bool(getattr(gateway, "enabled", False))
                    if gateway is not None
                    else False
                ),
                "orchestrator_present": orchestrator is not None,
                "execution_caps": {
                    "max_tool_invocations": applied_max_tool_invocations,
                    "tool_batch_cap": applied_tool_batch_cap,
                },
                "tool_use_progress": {
                    "enabled": show_tool_use_progress,
                    "request_id": request_id,
                },
                "tool_catalogue": tool_catalogue_summary,
            },
            "context_stats": {
                "sent_to_llm": context_stats,  # What was actually sent this turn
                "stored_context": current_context_stats,  # Current state after this turn
            },
            "tool_stats": tool_stats,  # MCP tool result statistics
            "tool_invocations": (
                [
                    {
                        "method": inv.get("tool") or inv.get("method", "unknown"),
                        "arguments": inv.get("payload") or inv.get("arguments", {}),
                        "error": inv.get("error"),
                    }
                    for inv in tool_invocations
                ]
                if tool_invocations
                else []
            ),
            "aux_llm_calls": auxiliary_llm_calls,
            "buttonify": buttonify_meta,
        }

        # Derive warnings from debug info and add to structure
        llm_debug_info["warnings"] = _derive_llm_debug_warnings(llm_debug_info)

        # Now save messages to history/context with debug info
        # Truncate large tool results to prevent context explosion
        truncated_tool_messages = _truncate_large_tool_results(
            tool_messages, max_tool_content_chars=5000
        )

        if user_concept_id:
            chat_history_service.add_message_to_history(
                user_concept_id, session_id, {"role": "user", "content": prompt_text}
            )
            for tool_msg in truncated_tool_messages:
                chat_history_service.add_message_to_history(
                    user_concept_id, session_id, tool_msg
                )
            # Save assistant message WITH debug data
            chat_history_service.add_message_to_history(
                user_concept_id,
                session_id,
                {"role": "assistant", "content": response_text},
                llm_debug_data=llm_debug_info,
            )
        else:
            current_app.config["CONTEXT"].append(
                {"role": "user", "content": prompt_text}
            )
            for tool_msg in truncated_tool_messages:
                current_app.config["CONTEXT"].append(tool_msg)
            current_app.config["CONTEXT"].append(
                {"role": "assistant", "content": response_text}
            )

        # Limit overall context size to prevent unbounded growth
        current_app.config["CONTEXT"] = _limit_context_size(
            current_app.config["CONTEXT"], max_messages=20
        )

        return jsonify(
            {
                "request_id": request_id,
                "response": response_text,
                "response_channels": (
                    {
                        "screen": presenter_channels.get("screen"),
                        "spoken": presenter_channels.get("spoken"),
                        "format": presenter_channels.get("format"),
                    }
                    if isinstance(presenter_channels, dict)
                    else None
                ),
                "llm_debug": llm_debug_info,
                "rag_trace": rag_trace,
            }
        )
    except Exception as e:
        print(f"Error during generation: {e}")  # Log error server-side
        # Return error with debug info showing the current turn only (not full context)
        # to avoid exponential token growth in debug data

        # Calculate context stats if available
        context_stats = None
        if "enhanced_context" in locals():
            try:
                context_stats = {
                    "sent_to_llm": _calculate_context_stats(enhanced_context)
                }
            except:
                pass

        if "show_tool_use_progress" in locals() and show_tool_use_progress:
            try:
                _set_tool_progress(
                    _get_tool_progress_scope_key(),
                    request_id if "request_id" in locals() else "unknown",
                    {
                        "status": "error",
                        "request_id": (
                            request_id if "request_id" in locals() else "unknown"
                        ),
                        "error": str(e),
                    },
                )
            except Exception:
                pass

        error_debug_info = {
            "interaction_timestamp_utc": interaction_timestamp_utc,
            "request_id": request_id,
            "model": model_name if "model_name" in locals() else "unknown",
            "messages": [{"role": "user", "content": prompt_text}],
            "response": None,
            "error": str(e),
            "context_stats": context_stats,
            "user_prompt": (
                user_prompt_debug if "user_prompt_debug" in locals() else None
            ),
            "tool_invocations": [],
        }
        error_debug_info["warnings"] = _derive_llm_debug_warnings(error_debug_info)
        body = {
            "request_id": request_id,
            "error": str(e),
            "llm_debug": error_debug_info,
        }
        if "rag_trace" in locals():
            body["rag_trace"] = rag_trace
        if "namespace_report" in locals():
            body["namespace_report"] = namespace_report
        return jsonify(body), 500


@von_bp.route("/history", methods=["GET"])
def history():
    """Retrieve chat history segments for the current user."""
    # Try to get user_id from session first, then query param
    user_concept_id = session.get("user_concept_id") or request.args.get("user_id")

    requested_session_id = request.args.get("session_id")
    if isinstance(requested_session_id, str) and requested_session_id.strip():
        session_id = requested_session_id.strip()
    else:
        # Ensure session_id exists (create if needed for the current session context)
        if "session_id" not in session:
            session["session_id"] = str(uuid.uuid4())
        session_id = session["session_id"]

    if not user_concept_id:
        return jsonify(
            {
                "history": [],
                "segments_returned": 0,
                "total_segments": 0,
                "has_more_history": False,
            }
        )

    requested_segments = request.args.get("segments", default=1, type=int)
    segment_count = max(1, requested_segments)
    segment_size = request.args.get("segment_size", default=None, type=int)
    if not segment_size or segment_size <= 0:
        segment_size = None
    tail_limit = request.args.get("tail_limit", default=None, type=int)
    if tail_limit is not None and tail_limit <= 0:
        tail_limit = None
    include_debug_raw = request.args.get("include_debug")
    include_debug = True
    if isinstance(include_debug_raw, str):
        parsed = include_debug_raw.strip().lower()
        if parsed in ("0", "false", "no", "n", "off"):
            include_debug = False
        elif parsed in ("1", "true", "yes", "y", "on"):
            include_debug = True
    history_tail_limit = None
    if isinstance(tail_limit, int) and tail_limit > 0:
        history_tail_limit = tail_limit
    elif isinstance(segment_size, int) and segment_size > 0:
        history_tail_limit = segment_size * max(segment_count, 1)

    try:
        namespace = chat_history_service.resolve_chat_history_namespace(user_concept_id)
        segments_result = chat_history_service.get_chat_history_segments(
            user_concept_id,
            session_id,
            include_locations=True,
            namespace=namespace,
            segment_size=segment_size,
            include_debug=include_debug,
            history_tail_limit=history_tail_limit,
            return_meta=True,
        )
        if isinstance(segments_result, tuple):
            segments, meta = segments_result
        else:
            segments = segments_result
            meta = {"history_truncated": False}
        total_segments = len(segments)

        if total_segments == 0:
            return jsonify(
                {
                    "history": [],
                    "segments_returned": 0,
                    "total_segments": 0,
                    "has_more_history": False,
                }
            )

        segment_count = min(segment_count, total_segments)
        selected_segments = segments[-segment_count:]
        flattened_history = [msg for segment in selected_segments for msg in segment]
        segments_returned = len(selected_segments)
        history_truncated = bool(meta.get("history_truncated"))
        has_more = history_truncated or segments_returned < total_segments
        if history_truncated and total_segments <= segments_returned:
            total_segments = segments_returned + 1

        return jsonify(
            {
                "history": flattened_history,
                "segments_returned": segments_returned,
                "total_segments": total_segments,
                "has_more_history": has_more,
            }
        )
    except Exception as e:
        print(f"Error retrieving history: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/history/debug", methods=["GET"])
def history_debug():
    """Retrieve stored LLM debug data for a specific history entry."""
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return jsonify({"error": "Not authenticated"}), 401

    session_id = request.args.get("session_id") or session.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return jsonify({"error": "session_id required"}), 400

    history_index = request.args.get("history_index", default=None, type=int)
    if history_index is None or history_index < 0:
        return jsonify({"error": "history_index required"}), 400

    try:
        namespace = chat_history_service.resolve_chat_history_namespace(user_concept_id)
        debug_data = chat_history_service.get_chat_history_debug_entry(
            user_id=user_concept_id,
            session_id=session_id.strip(),
            history_index=history_index,
            namespace=namespace,
        )
        if not debug_data:
            return jsonify(
                {
                    "success": False,
                    "error": "debug_not_available",
                    "history_location": {
                        "session_id": session_id.strip(),
                        "history_index": history_index,
                    },
                }
            )
        return jsonify(
            {
                "success": True,
                "history_location": {
                    "session_id": session_id.strip(),
                    "history_index": history_index,
                },
                "llm_debug_data": debug_data,
            }
        )
    except Exception as e:
        print(f"Error retrieving history debug data: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/history/backfill_spoken", methods=["POST"])
def history_backfill_spoken():
    """Generate and persist missing <spoken> talk track for a stored assistant turn.

    This is intended for legacy history turns where presenter channels were not
    generated or persisted at the time. The backfill:
    - requires authentication
    - does not touch session `updated_at` (so it won't reorder session recency)
    """

    from ...security.access_control import get_effective_user_concept_id

    user_concept_id = get_effective_user_concept_id()
    if not user_concept_id:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    history_location = data.get("history_location")
    if not isinstance(history_location, dict):
        history_location = {}

    target_session_id = history_location.get("session_id") or data.get("session_id")
    history_index = history_location.get("history_index")
    if history_index is None:
        history_index = data.get("history_index")

    if not isinstance(target_session_id, str) or not target_session_id.strip():
        return jsonify({"error": "session_id is required"}), 400
    if not isinstance(history_index, int) or history_index < 0:
        return jsonify({"error": "history_index must be a non-negative integer"}), 400

    force = bool(data.get("force"))

    # Fetch the stored assistant message and associated previous user prompt.
    try:
        chat_history_coll = chat_history_service.get_chat_history_collection_service()
        if chat_history_coll is None:
            return (
                jsonify({"error": "Could not connect to chat history collection."}),
                500,
            )

        doc = chat_history_coll.find_one(
            {"user_id": user_concept_id, "session_id": target_session_id},
            {"history": 1},
        )
        history = (doc or {}).get("history") or []
        if not isinstance(history, list) or history_index >= len(history):
            return jsonify({"error": "History entry not found"}), 404

        entry = history[history_index]
        if not isinstance(entry, dict) or entry.get("role") != "assistant":
            return (
                jsonify({"error": "Target history entry is not an assistant message"}),
                400,
            )

        screen_text = entry.get("content")
        if not isinstance(screen_text, str) or not screen_text.strip():
            return jsonify({"error": "Assistant message has no content"}), 400
        screen_text = screen_text.strip()

        existing_debug = entry.get("llm_debug_data")
        existing_channels = None
        if isinstance(existing_debug, dict):
            existing_channels = existing_debug.get("presenter_channels")
        if not force and isinstance(existing_channels, dict):
            spoken_existing = existing_channels.get("spoken")
            if isinstance(spoken_existing, str) and spoken_existing.strip():
                return jsonify(
                    {
                        "status": "already_present",
                        "presenter_channels": existing_channels,
                        "updated": False,
                    }
                )

        prompt_text = ""
        for i in range(history_index - 1, -1, -1):
            msg = history[i]
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "system" and msg.get("content") == "__RESET__":
                # Stop at reset boundary.
                break
            if msg.get("role") == "user":
                candidate = msg.get("content")
                if isinstance(candidate, str) and candidate.strip():
                    prompt_text = candidate.strip()
                break

    except Exception as e:
        return jsonify({"error": f"Failed reading history: {e}"}), 500

    # Load narration prompt fragments (best-effort).
    narration_prompt_text = None
    try:
        from ...services.chat_auxiliary_prompt_service import (
            get_user_specific_prompt_fragments,
        )

        narration_prompt_fragments = get_user_specific_prompt_fragments(
            user_concept_id,
            prompt_types=("#V#von_chat_narration_prompt",),
        )
        narration_texts = []
        for frag in narration_prompt_fragments:
            if not isinstance(frag, dict):
                continue
            content = frag.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            narration_texts.append(content)
        narration_prompt_text = "\n\n".join(
            text.strip() for text in narration_texts if text and text.strip()
        )
        narration_prompt_text = (
            narration_prompt_text.strip() if narration_prompt_text else None
        )
    except Exception:
        narration_prompt_text = None

    # Generate spoken talk track.
    try:
        org_concept_id = session.get("organisation_concept_id") if session else None
        llm_client = get_llm_client(
            user_concept_id=user_concept_id, org_concept_id=org_concept_id
        )
        model_name = get_active_model_name()

        def _extract_spoken_only(text: str) -> str | None:
            if not isinstance(text, str) or not text:
                return None
            import re

            m = re.search(
                r"<spoken>\s*(.*?)\s*</spoken>",
                text,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if not m:
                return None
            value = m.group(1)
            if not isinstance(value, str):
                return None
            cleaned = value.strip()
            return cleaned or None

        # Include a small client-reported timing hint for narration generation.
        # (Non-authoritative; used only for guidance.)
        timing_hint = None
        try:
            from ...services.client_capabilities_service import (
                get_client_capabilities_snapshot,
            )

            snapshot = get_client_capabilities_snapshot()
            speech = (
                snapshot.get("speech_synthesis") if isinstance(snapshot, dict) else None
            )
            speech = speech if isinstance(speech, dict) else {}
            raw_settings = speech.get("settings")
            settings = raw_settings if isinstance(raw_settings, dict) else {}

            preferred = settings.get("preferred_speaking_seconds")
            maximum = settings.get("max_speaking_seconds")

            try:
                preferred_int = int(preferred) if preferred is not None else None
            except Exception:
                preferred_int = None

            try:
                maximum_int = int(maximum) if maximum is not None else None
            except Exception:
                maximum_int = None

            if preferred_int is not None:
                preferred_int = max(1, min(preferred_int, 600))
            if maximum_int is not None:
                maximum_int = max(1, min(maximum_int, 600))

            effective_preferred = preferred_int
            if preferred_int is not None and maximum_int is not None:
                effective_preferred = min(preferred_int, maximum_int)

            if effective_preferred is not None or maximum_int is not None:
                timing_hint = (
                    "Speech timing hint (client-reported, non-authoritative): "
                    f"preferred_speaking_seconds={effective_preferred!r}, "
                    f"max_speaking_seconds={maximum_int!r}. "
                    "Aim for about preferred_speaking_seconds seconds and do not exceed max_speaking_seconds."
                )
        except Exception:
            timing_hint = None

        narration_system = (
            "You are Von. Produce a short talk track for text-to-speech. "
            "Return ONLY one block: <spoken>...</spoken>. "
            "Do not include <screen>. Do not include code blocks. "
            "Use New Zealand English spelling."
            + ("\n\n" + timing_hint if timing_hint else "")
            + (
                "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
                + narration_prompt_text
                if narration_prompt_text
                else ""
            )
        )

        narration_user = (
            "User message:\n"
            f"{prompt_text}\n\n"
            "On-screen content (do not read verbatim if long; summarise):\n"
            f"{screen_text}\n"
        )

        narration_response = llm_client.generate(
            prompt="Generate <spoken> talk track",
            context=[
                {"role": "system", "content": narration_system},
                {"role": "user", "content": narration_user},
            ],
            model=model_name,
        )

        spoken = _extract_spoken_only(str(narration_response))
        if not spoken:
            return jsonify({"status": "no_spoken_generated", "updated": False}), 200

        presenter_channels = {
            "screen": screen_text,
            "spoken": spoken,
            "format": "narration_fallback_v1",
        }

        update_result = (
            chat_history_service.upsert_presenter_channels_for_history_message(
                user_id=user_concept_id,
                session_id=target_session_id,
                history_index=history_index,
                presenter_channels=presenter_channels,
                generated_at=datetime.now(timezone.utc),
                force=force,
            )
        )

        return jsonify(
            {
                "status": "ok",
                "presenter_channels": presenter_channels,
                "updated": bool(update_result.get("updated")),
                "matched": bool(update_result.get("matched")),
            }
        )
    except Exception as e:
        return jsonify({"error": f"Failed generating spoken talk track: {e}"}), 500


@von_bp.route("/history/length", methods=["GET"])
def history_length():
    """Retrieve the chat history length for the current user."""
    user_concept_id = session.get("user_concept_id")

    if not user_concept_id:
        return jsonify({"history_length": 0, "authenticated": False})

    try:
        namespace = chat_history_service.resolve_chat_history_namespace(user_concept_id)
        length = chat_history_service.get_chat_history_length(
            user_concept_id, namespace=namespace
        )
        session_count = chat_history_service.get_chat_history_session_count(
            user_concept_id, namespace=namespace
        )
        return jsonify(
            {
                "history_length": length,
                "session_count": session_count,
                "authenticated": True,
            }
        )
    except Exception as e:
        print(f"Error retrieving history length: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/history/sessions", methods=["GET"])
def history_sessions():
    """Return per-session chat history counts for the current user."""

    user_concept_id = session.get("user_concept_id")
    if not user_concept_id:
        return jsonify({"authenticated": False, "sessions": []})

    limit = request.args.get("limit", default=50, type=int)
    summary_mode = request.args.get("summary", default="full")
    if not isinstance(summary_mode, str) or not summary_mode.strip():
        summary_mode = "full"
    try:
        namespace = chat_history_service.resolve_chat_history_namespace(user_concept_id)
        sessions = chat_history_service.get_chat_history_session_summaries(
            user_concept_id,
            limit=limit,
            namespace=namespace,
            summary_mode=summary_mode,
        )
        return jsonify(
            {
                "authenticated": True,
                "sessions": sessions,
                "active_session_id": session.get("session_id"),
            }
        )
    except Exception as e:
        print(f"Error retrieving history sessions: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/models", methods=["GET"])
def get_models():
    """API endpoint to fetch the list of available models."""
    # Assuming list_models_func is stored in app config or accessible globally
    list_models_func = current_app.config.get("LIST_MODELS_FUNC")
    if not list_models_func:
        return jsonify({"error": "Model listing function not configured."}), 500
    try:
        models = list_models_func()
        return jsonify(models), 200
    except Exception as e:
        print(f"Error fetching models: {e}")  # Log error server-side
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/search", methods=["GET"])
def search_concepts_endpoint():
    """API endpoint to search for concepts by name.

    Query parameters:
    - q: Search query string (required)
    - limit: Maximum results to return (default: 8)
    """
    try:
        from ...services.concept_search_service import search_concepts

        query = request.args.get("q", "").strip()
        limit = request.args.get("limit", default=8, type=int)

        if not query:
            return jsonify({"results": [], "total_count": 0}), 200

        result = search_concepts(
            query=query, match_type="substring", limit=limit, include_description=False
        )

        # Format results for autocomplete
        formatted_results = [
            {
                "id": item.get("concept_id"),  # Frontend expects 'id' field
                "concept_id": item.get("concept_id"),
                "name": item.get("name") or item.get("concept_id"),
                "kind": item.get("kind", "unknown"),
            }
            for item in result.get("results", [])
        ]

        return (
            jsonify(
                {
                    "results": formatted_results,
                    "total_count": result.get("total_count", 0),
                }
            ),
            200,
        )
    except Exception as e:
        current_app.logger.error(f"Concept search error: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/render_markdown", methods=["POST"])
def render_markdown_endpoint():
    """Render markdown to sanitised HTML.

    Request JSON:
    - text: markdown string

    Response JSON:
    - html: sanitised HTML string
    """

    try:
        from ...services.markdown_render_service import render_markdown_to_safe_html

        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        if not isinstance(text, str):
            return jsonify({"error": "Field 'text' must be a string"}), 400

        if len(text) > 200_000:
            return jsonify({"error": "Markdown payload too large"}), 413

        html = render_markdown_to_safe_html(text)
        return jsonify({"html": html}), 200
    except Exception as e:
        current_app.logger.error(f"render_markdown error: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/reset", methods=["POST"])
def reset_context():
    """Reset the conversation context."""
    try:
        user_concept_id = session.get("user_concept_id")
        session_id = session.get("session_id")

        if user_concept_id and session_id:
            chat_history_service.add_reset_marker_to_history(
                user_concept_id, session_id
            )

        # Clear the conversation context
        current_app.config["CONTEXT"] = []
        print("Context reset successfully")  # Log server-side
        return (
            jsonify({"status": "reset", "message": "Context reset successfully"}),
            200,
        )
    except Exception as e:
        print(f"Error resetting context: {e}")  # Log error server-side
        return jsonify({"error": f"Failed to reset context: {str(e)}"}), 500


# Phase 2: Organisation and Role Selection Endpoints
@von_bp.route("/api/session/set_user_concept", methods=["POST"])
def set_user_concept():
    """Set the current user concept in the session.

    This aligns the authenticated session identity with the user selected in Settings.

    Request body: {user_concept_id: str}
    Returns: {user_id, organisation_id, role, namespace, status: 'updated'}
    """
    try:
        from ...services.namespace_service import derive_namespace

        authenticated_id = (
            session.get("user_id")
            or session.get("user_concept_id")
            or session.get("user_email")
        )
        if not authenticated_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        user_concept_id = data.get("user_concept_id")
        if not isinstance(user_concept_id, str) or not user_concept_id.strip():
            return jsonify({"error": "user_concept_id required"}), 400

        user_concept_id = user_concept_id.strip()
        if not user_concept_id.startswith("#V#"):
            user_concept_id = f"#V#{user_concept_id}"

        user_slug = user_concept_id[3:]
        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        existing_user_concept_id = session.get("user_concept_id")
        if isinstance(existing_user_concept_id, str):
            existing_user_concept_id = existing_user_concept_id.strip()
            if existing_user_concept_id and not existing_user_concept_id.startswith(
                "#V#"
            ):
                existing_user_concept_id = f"#V#{existing_user_concept_id}"
        else:
            existing_user_concept_id = None

        organisation_concept_id = session.get("organisation_concept_id")
        role_in_org = session.get("role_in_org")
        org_slug = None
        if isinstance(organisation_concept_id, str) and organisation_concept_id.strip():
            org_slug_raw = organisation_concept_id.strip()
            if org_slug_raw.startswith("#V#"):
                org_slug_raw = org_slug_raw[3:]
            if "@" in org_slug_raw:
                org_slug_raw = org_slug_raw.split("@", 1)[0]
            if "+" in org_slug_raw:
                org_slug_raw = org_slug_raw.split("+", 1)[0]
            org_slug = re.sub(r"[^a-z0-9]+", "_", org_slug_raw.strip().lower()).strip(
                "_"
            )

        namespace = derive_namespace(user_slug, org_slug, role_in_org)

        # Store the concept id for authoritative identity.
        session["user_concept_id"] = user_concept_id
        # Keep backward compatibility with code that still reads session['user_id'].
        session["user_id"] = user_concept_id

        # Clear org-scoped context only when switching between different user concepts.
        # If we are simply backfilling user_concept_id for an already-authenticated session,
        # keep any existing org selection and recompute namespace accordingly.
        if existing_user_concept_id and existing_user_concept_id != user_concept_id:
            organisation_concept_id = None
            role_in_org = None
            session.pop("organisation_concept_id", None)
            session.pop("role_in_org", None)
        session["namespace"] = namespace
        session.modified = True

        organisation_id_response = None
        if isinstance(organisation_concept_id, str) and organisation_concept_id.strip():
            organisation_id_response = organisation_concept_id.strip()
            if not organisation_id_response.startswith("#V#"):
                organisation_id_response = f"#V#{organisation_id_response}"

        return (
            jsonify(
                {
                    "status": (
                        "updated"
                        if existing_user_concept_id != user_concept_id
                        else "unchanged"
                    ),
                    "user_id": user_concept_id,
                    "organisation_id": organisation_id_response,
                    "role": role_in_org,
                    "namespace": namespace,
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error setting user concept: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/set_organisation", methods=["POST"])
def set_organisation():
    """
    Set the current organisation context in the session.

    Request body: {organisation_concept_id: str} or empty dict to clear
    - organisation_concept_id can be a concept ID with or without #V# prefix
    - If organisation_concept_id is present but empty/null, treat as clear request
    Returns: {user_id, organisation_id, role, namespace, status: 'updated'}
    """
    try:
        from ...services.namespace_service import derive_namespace
        from ...security.role_resolver import get_user_role

        user_id = (
            session.get("user_concept_id")
            or session.get("user_id")
            or session.get("user_email")
        )
        if not user_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json() or {}
        org_id = data.get("organisation_concept_id")

        # Normalise user id to a safe slug for namespace/role lookup
        user_slug = str(user_id)
        if user_slug.startswith("#V#"):
            user_slug = user_slug[3:]
        if "@" in user_slug:
            user_slug = user_slug.split("@", 1)[0]
        if "+" in user_slug:
            user_slug = user_slug.split("+", 1)[0]
        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        # Check if this is a clear request (empty dict or explicit null/empty string)
        is_clear_request = "organisation_concept_id" in data and not org_id

        # Handle clearing (personal context / no org)
        if is_clear_request:
            namespace = derive_namespace(user_slug)
            session.pop("organisation_concept_id", None)
            session.pop("role_in_org", None)
            session["namespace"] = namespace
            session.modified = True
            return (
                jsonify(
                    {
                        "status": "updated",
                        "user_id": user_id,
                        "organisation_id": None,
                        "role": None,
                        "namespace": namespace,
                    }
                ),
                200,
            )

        # If org_id not provided and not an explicit clear, that's an error
        if not org_id:
            return jsonify({"error": "organisation_concept_id required"}), 400

        # TODO: Validate user is member of org (once membership model exists)
        # For now, allow any org switch

        # org_id may arrive as a concept id (e.g., "#V#university_of_auckland_strong_ai_lab")
        org_slug = str(org_id)
        if org_slug.startswith("#V#"):
            org_slug = org_slug[3:]
        org_slug = org_slug.strip().lower().replace(" ", "_")

        # Get role for this user in this org (stub resolver expects slugs)
        try:
            role_in_org = get_user_role(user_slug, org_slug)
        except Exception:
            role_in_org = "member"  # Default fallback

        # Derive composite namespace using slug values
        namespace = derive_namespace(user_slug, org_slug)

        # Update session (store slug form for backward compatibility)
        session["organisation_concept_id"] = org_slug
        session["role_in_org"] = role_in_org
        session["namespace"] = namespace
        session.modified = True

        # Return concept ID form in API response (with #V# prefix)
        concept_id_response = (
            f"#V#{org_slug}" if not str(org_id).startswith("#V#") else org_id
        )

        return (
            jsonify(
                {
                    "status": "updated",
                    "user_id": user_id,
                    "organisation_id": concept_id_response,
                    "role": role_in_org,
                    "namespace": namespace,
                }
            ),
            200,
        )

    except Exception as e:
        print(f"Error setting organisation: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/context", methods=["GET"])
def get_session_context():
    """
    Get current session context (user, organisation, role, namespace).

    Returns: {user_id, organisation_id, role, namespace, authenticated}
    """
    try:
        from ...services.namespace_service import derive_namespace
        from ...security.role_resolver import get_user_role

        user_id = (
            session.get("user_concept_id")
            or session.get("user_id")
            or session.get("user_email")
        )
        if not user_id:
            return (
                jsonify(
                    {
                        "authenticated": False,
                        "user_id": None,
                        "organisation_id": None,
                        "role": None,
                        "namespace": None,
                    }
                ),
                200,
            )

        org_id = session.get("organisation_concept_id")
        role_in_org = session.get("role_in_org")
        namespace = session.get("namespace")

        # If no namespace in session, derive it
        if not namespace:
            user_slug = str(user_id)
            if user_slug.startswith("#V#"):
                user_slug = user_slug[3:]
            if "@" in user_slug:
                user_slug = user_slug.split("@", 1)[0]
            if "+" in user_slug:
                user_slug = user_slug.split("+", 1)[0]
            user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")
            if org_id:
                # Get role if not in session
                if not role_in_org:
                    try:
                        role_in_org = get_user_role(user_slug, org_id)
                    except Exception:
                        role_in_org = "member"
                namespace = derive_namespace(user_slug, org_id)
            else:
                namespace = derive_namespace(user_slug)

        organisation_id_response = None
        if isinstance(org_id, str) and org_id.strip():
            organisation_id_response = org_id.strip()
            if not organisation_id_response.startswith("#V#"):
                organisation_id_response = f"#V#{organisation_id_response}"

        return (
            jsonify(
                {
                    "authenticated": True,
                    "user_id": user_id,
                    "organisation_id": organisation_id_response,
                    "role": role_in_org,
                    "namespace": namespace,
                }
            ),
            200,
        )

    except Exception as e:
        print(f"Error getting session context: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/set_chat_session", methods=["POST"])
def set_chat_session():
    """Set the active chat session_id for the current authenticated user.

    Request body: {session_id: str, include_history?: bool}
    Returns: {status, session_id, session_name, history}

    This enables the frontend to switch to a prior session and continue it.
    """
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()
        include_history = data.get("include_history", True)
        if isinstance(include_history, bool):
            pass
        elif isinstance(include_history, str):
            parsed = include_history.strip().lower()
            if parsed in ("0", "false", "no", "n", "off"):
                include_history = False
            elif parsed in ("1", "true", "yes", "y", "on"):
                include_history = True
            else:
                include_history = True
        elif isinstance(include_history, (int, float)):
            include_history = include_history != 0
        else:
            include_history = True

        # Verify the session belongs to this user.
        coll = chat_history_service.get_chat_history_collection_service()
        if coll is None:
            return jsonify({"error": "Chat history unavailable"}), 503

        namespace = chat_history_service.resolve_chat_history_namespace(user_concept_id)
        query = chat_history_service.build_chat_history_query(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=namespace,
        )
        projection = {"session_name": 1}
        if include_history:
            projection["history"] = 1
        doc = coll.find_one(query, projection)
        if not doc:
            return jsonify({"error": "Session not found"}), 404

        session_name = doc.get("session_name")

        def _normalise_timestamp(value):
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.isoformat().replace("+00:00", "Z")
            return value

        normalised_history = []
        if include_history:
            history = doc.get("history") or []
            if not isinstance(history, list):
                history = []
            for msg in history:
                if not isinstance(msg, dict):
                    continue
                out = dict(msg)
                if "timestamp" in out:
                    out["timestamp"] = _normalise_timestamp(out.get("timestamp"))
                normalised_history.append(out)

        # Switch active session.
        session["session_id"] = session_id
        session.modified = True

        # Clear any non-persistent context cache.
        try:
            current_app.config["CONTEXT"] = []
        except Exception:
            pass

        return (
            jsonify(
                {
                    "status": "updated",
                    "session_id": session_id,
                    "session_name": session_name,
                    "history": normalised_history,
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error setting chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/create_chat_session", methods=["POST"])
def create_chat_session():
    """Create and switch to a new named chat session for the current user."""
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_name = (
            data.get("session_name") or data.get("name") or data.get("chat_name")
        )

        session_id = str(uuid.uuid4())

        result = chat_history_service.create_chat_session(
            user_id=user_concept_id,
            session_id=session_id,
            session_name=session_name,
        )

        session["session_id"] = session_id
        session.modified = True

        try:
            current_app.config["CONTEXT"] = []
        except Exception:
            pass

        return (
            jsonify(
                {
                    "status": "created",
                    "session_id": session_id,
                    "session_name": result.get("session_name"),
                    "history": [],
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error creating chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/rename_chat_session", methods=["POST"])
def rename_chat_session():
    """Rename an existing chat session for the current user."""
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()
        include_history = data.get("include_history", True)
        if isinstance(include_history, bool):
            pass
        elif isinstance(include_history, str):
            parsed = include_history.strip().lower()
            if parsed in ("0", "false", "no", "n", "off"):
                include_history = False
            elif parsed in ("1", "true", "yes", "y", "on"):
                include_history = True
            else:
                include_history = True
        elif isinstance(include_history, (int, float)):
            include_history = include_history != 0
        else:
            include_history = True

        session_name = data.get("session_name") or data.get("name")
        if not isinstance(session_name, str) or not session_name.strip():
            return jsonify({"error": "session_name required"}), 400

        namespace = chat_history_service.resolve_chat_history_namespace(user_concept_id)
        result = chat_history_service.rename_chat_session(
            user_id=user_concept_id,
            session_id=session_id,
            session_name=session_name,
            namespace=namespace,
        )

        if not result.get("matched"):
            return jsonify({"error": "Session not found"}), 404

        return (
            jsonify(
                {
                    "status": "updated",
                    "session_id": session_id,
                    "session_name": result.get("session_name"),
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error renaming chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/chat_session_links", methods=["GET", "POST"])
def chat_session_links():
    """Get or set concept links for a chat session.

    Stored on the chat_history session document as `session_links`.
    Links are many-to-many lists of concept_ids:
      - programmes
      - projects
      - activities
      - modalities
    """
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        requested_session_id = (
            request.args.get("session_id")
            if request.method == "GET"
            else (request.get_json(silent=True) or {}).get("session_id")
        )
        session_id = None
        if isinstance(requested_session_id, str) and requested_session_id.strip():
            session_id = requested_session_id.strip()
        else:
            session_id = session.get("session_id")

        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        namespace = chat_history_service.resolve_chat_history_namespace(user_concept_id)

        # Best-effort: ensure modality concepts exist so the UI can attach them.
        try:
            from ...vontology.utils_vontology import (
                ensure_conversation_modality_concepts,
            )

            ensure_conversation_modality_concepts()
        except Exception:
            pass

        if request.method == "GET":
            links = chat_history_service.get_chat_session_links(
                user_id=user_concept_id,
                session_id=session_id,
                namespace=namespace,
            )
            return (
                jsonify(
                    {
                        "status": "ok",
                        "session_id": session_id,
                        "session_links": links,
                    }
                ),
                200,
            )

        data = request.get_json(silent=True) or {}
        raw_links = data.get("session_links")
        if not isinstance(raw_links, dict):
            # Allow top-level key alternatives for callers.
            raw_links = {
                "programmes": data.get("programmes") or data.get("programme_ids"),
                "projects": data.get("projects") or data.get("project_ids"),
                "activities": data.get("activities") or data.get("activity_ids"),
                "modalities": data.get("modalities") or data.get("modality_ids"),
            }

        # Filter out unknown concept IDs (best-effort) so we don't persist stale references.
        try:
            from ...db.repositories.concepts_repository import ConceptsRepository

            def _flatten(values):
                if values is None:
                    return []
                if isinstance(values, str):
                    return [values]
                if isinstance(values, list):
                    return values
                return []

            all_ids = []
            for key in ("programmes", "projects", "activities", "modalities"):
                all_ids.extend(_flatten(raw_links.get(key)))

            all_ids = [
                v.strip()
                for v in all_ids
                if isinstance(v, str) and v.strip().startswith("#V#")
            ]
            if all_ids:
                found = set(
                    doc.get("concept_id")
                    for doc in ConceptsRepository.find(
                        {"concept_id": {"$in": list(set(all_ids))}},
                        {"concept_id": 1},
                    )
                    if isinstance(doc, dict) and isinstance(doc.get("concept_id"), str)
                )
            else:
                found = set()

            missing = sorted({cid for cid in set(all_ids) if cid not in found})
            if found:
                for key in ("programmes", "projects", "activities", "modalities"):
                    raw_links[key] = [
                        v.strip()
                        for v in _flatten(raw_links.get(key))
                        if isinstance(v, str)
                        and v.strip().startswith("#V#")
                        and v.strip() in found
                    ]
        except Exception:
            missing = []

        result = chat_history_service.set_chat_session_links(
            user_id=user_concept_id,
            session_id=session_id,
            session_links=raw_links,
            namespace=namespace,
        )

        if not result.get("matched"):
            return jsonify({"error": "Session not found"}), 404

        body = {
            "status": "updated" if result.get("updated") else "ok",
            "session_id": session_id,
            "session_links": result.get("session_links") or {},
        }
        if missing:
            body["missing_concepts"] = missing
        return jsonify(body), 200
    except Exception as e:
        print(f"Error updating chat session links: {e}")
        return jsonify({"error": str(e)}), 500


def _normalise_concept_id(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if not value.startswith("#V#"):
        value = f"#V#{value}"
    return value


def _collect_relationship_concept_ids(concept: dict) -> set[str]:
    relationships = concept.get("relationships") if isinstance(concept, dict) else None
    if not isinstance(relationships, dict):
        return set()
    found: set[str] = set()
    for value in relationships.values():
        if isinstance(value, str):
            cid = _normalise_concept_id(value)
            if cid:
                found.add(cid)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    cid = _normalise_concept_id(item)
                    if cid:
                        found.add(cid)
    return found


def _build_invitee_entry(concept: dict, *, role: str, match_details: dict) -> dict:
    from ...services.concept_service import enrich_concept_with_text_relations
    from ...vontology.utils_vontology import (
        get_concept_display_name_with_names_fallback,
    )

    enriched = enrich_concept_with_text_relations(concept)
    display_name = get_concept_display_name_with_names_fallback(enriched)
    return {
        "concept_id": concept.get("concept_id"),
        "name": display_name or concept.get("name") or "Unknown",
        "role": role,
        "match": match_details,
    }


@von_bp.route("/api/shared_conversations/invitees", methods=["GET"])
def get_shared_conversation_invitees():
    """List eligible invitees for a shared conversation.

    Returns organisation-scoped users, ordered by relevance to session links.
    """
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services import chat_history_service
        from ...services.organisation_membership_service import (
            get_organisation_members,
            get_user_memberships,
        )
        from ...services.concept_service import get_concept_by_concept_id
        from ...services.shared_conversation_service import get_invite_status_map

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        session_id = request.args.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        organisation_concept_id = session.get("organisation_concept_id")
        organisation_concept_id = _normalise_concept_id(organisation_concept_id)
        if not organisation_concept_id:
            return jsonify({"error": "No organisation context"}), 400

        memberships = get_user_memberships(user_concept_id)
        if not any(
            m.get("organisation_concept_id") == organisation_concept_id
            for m in memberships.get("memberships", [])
        ):
            return jsonify({"error": "Not authorised for organisation"}), 403

        namespace = chat_history_service.resolve_chat_history_namespace(user_concept_id)
        session_links = chat_history_service.get_chat_session_links(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=namespace,
        )

        link_sets = {
            key: set(session_links.get(key) or [])
            for key in ("programmes", "projects", "activities", "modalities")
        }

        org_members = get_organisation_members(organisation_concept_id)
        candidates = []
        invitee_ids: list[str] = []
        for member in org_members.get("members", []):
            candidate_id = member.get("user_concept_id")
            candidate_id = _normalise_concept_id(candidate_id)
            if not candidate_id or candidate_id == user_concept_id:
                continue
            concept = get_concept_by_concept_id(concept_id=candidate_id)
            if not isinstance(concept, dict):
                continue

            related_ids = _collect_relationship_concept_ids(concept)
            match_details: dict[str, Any] = {
                key: sorted(link_sets[key] & related_ids) for key in link_sets
            }
            match_score = sum(len(vals) for vals in match_details.values())
            match_details["score"] = match_score

            entry = _build_invitee_entry(
                concept,
                role=member.get("role") or "member",
                match_details=match_details,
            )
            candidates.append(entry)
            invitee_ids.append(candidate_id)

        status_map = get_invite_status_map(
            session_id=session_id, invitee_ids=invitee_ids
        )
        for entry in candidates:
            invitee_id = entry.get("concept_id")
            if invitee_id and invitee_id in status_map:
                entry["invite_status"] = status_map[invitee_id]

        candidates.sort(
            key=lambda item: (
                -(item.get("match", {}).get("score") or 0),
                (item.get("name") or "").lower(),
            )
        )

        return (
            jsonify(
                {
                    "invitees": candidates,
                    "total_count": len(candidates),
                    "session_links": session_links,
                    "ordered_by": "conversation_links",
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error listing invitees: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/invite", methods=["POST"])
def invite_to_shared_conversation():
    """Create a shared conversation invite for a session."""
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.organisation_membership_service import (
            get_organisation_members,
            get_user_memberships,
        )
        from ...services.shared_conversation_service import create_invite
        from ...services.episode_logging_service import log_episode

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        invitee_concept_id = _normalise_concept_id(
            data.get("invitee_concept_id") or data.get("invitee_user_id")
        )

        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        if not invitee_concept_id:
            return jsonify({"error": "invitee_concept_id required"}), 400

        organisation_concept_id = _normalise_concept_id(
            session.get("organisation_concept_id")
        )
        if not organisation_concept_id:
            return jsonify({"error": "No organisation context"}), 400

        memberships = get_user_memberships(user_concept_id)
        if not any(
            m.get("organisation_concept_id") == organisation_concept_id
            for m in memberships.get("memberships", [])
        ):
            return jsonify({"error": "Not authorised for organisation"}), 403

        org_members = get_organisation_members(organisation_concept_id)
        valid_ids = {
            _normalise_concept_id(m.get("user_concept_id"))
            for m in org_members.get("members", [])
        }
        if invitee_concept_id not in valid_ids:
            return jsonify({"error": "Invitee not in organisation"}), 403

        result = create_invite(
            session_id=session_id,
            inviter_user_id=user_concept_id,
            invitee_user_id=invitee_concept_id,
            organisation_concept_id=organisation_concept_id,
        )

        log_episode(
            episode_type="shared_conversation_invite_created",
            actor_user_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            session_id=session_id,
            related_invite_id=result.get("invite", {}).get("invite_id"),
            payload={
                "invitee_user_id": invitee_concept_id,
                "created": bool(result.get("created")),
            },
            status="created" if result.get("created") else "exists",
        )

        return jsonify({"status": "ok", **result}), 200
    except Exception as e:
        print(f"Error creating invite: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/invites", methods=["GET"])
def list_shared_conversation_invites():
    """List incoming invites for the authenticated user."""
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.shared_conversation_service import list_invites_for_user

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        status = request.args.get("status") or "pending"
        session_id = request.args.get("session_id")

        invites = list_invites_for_user(
            user_concept_id=user_concept_id,
            status=status,
            direction="incoming",
            session_id=session_id,
        )
        return jsonify({"invites": invites, "total_count": len(invites)}), 200
    except Exception as e:
        print(f"Error listing invites: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/invites/respond", methods=["POST"])
def respond_shared_conversation_invite():
    """Accept or decline a shared conversation invite."""
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.shared_conversation_service import respond_to_invite
        from ...services.episode_logging_service import log_episode

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        invite_id = data.get("invite_id")
        action = data.get("action")
        if not isinstance(invite_id, str) or not invite_id.strip():
            return jsonify({"error": "invite_id required"}), 400

        updated = respond_to_invite(
            invite_id=invite_id.strip(),
            user_concept_id=user_concept_id,
            action=action or "",
        )
        if not updated:
            return jsonify({"error": "Invite not found"}), 404

        log_episode(
            episode_type="shared_conversation_invite_responded",
            actor_user_id=user_concept_id,
            organisation_concept_id=updated.get("organisation_concept_id"),
            session_id=updated.get("session_id"),
            related_invite_id=invite_id.strip(),
            payload={"action": action},
            status=updated.get("status"),
        )

        return jsonify({"status": "ok", "invite": updated}), 200
    except Exception as e:
        print(f"Error responding to invite: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/organisations/my_organisations", methods=["GET"])
def get_my_organisations():
    """
    Get list of organisations the user is member of.

    Returns: {organisations: [{concept_id, name, role}, ...], total_count}
    """
    try:
        from ...security.role_resolver import get_all_user_organisations, get_user_role
        from ...services.concept_service import get_concept_by_concept_id

        user_id = (
            session.get("user_id")
            or session.get("user_concept_id")
            or session.get("user_email")
        )
        if not user_id:
            return jsonify({"error": "Not authenticated"}), 401

        requested_user_concept_id = request.args.get("user_concept_id")
        user_concept_id = requested_user_concept_id or session.get("user_concept_id")
        user_email = session.get("user_email")

        def _normalise_relationships(rel):
            if not isinstance(rel, (dict, list)):
                return {}
            if isinstance(rel, list):
                out = {}
                for item in rel:
                    if not isinstance(item, dict):
                        continue
                    pred = item.get("predicate")
                    tgt = item.get("target")
                    if not pred or not tgt:
                        continue
                    out.setdefault(pred, [])
                    if isinstance(tgt, list):
                        out[pred].extend(tgt)
                    else:
                        out[pred].append(tgt)
                return out
            return rel or {}

        def _prettify_concept_id(concept_id: str) -> str:
            return concept_id.replace("#V#", "").replace("_", " ").title()

        # Derive a slug for stub role resolution.
        # Prefer identifiers that are stable/meaningful (concept ID or email) over
        # opaque auth subjects.
        slug_source = user_concept_id or user_email or user_id

        user_slug = str(slug_source)
        if user_slug.startswith("#V#"):
            user_slug = user_slug[3:]
        if "@" in user_slug:
            user_slug = user_slug.split("@", 1)[0]
        if "+" in user_slug:
            user_slug = user_slug.split("+", 1)[0]

        import re

        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        USER_PREF_ORG_PREDICATE = "#V#member_of_organisation"

        organisations = []

        # Prefer memberships stored on the selected/authenticated user concept.
        if isinstance(user_concept_id, str) and user_concept_id.strip():
            try:
                user_concept = get_concept_by_concept_id(concept_id=user_concept_id)
            except Exception:
                user_concept = None
            if isinstance(user_concept, dict):
                rel = _normalise_relationships(user_concept.get("relationships", {}))
                org_raw = rel.get(USER_PREF_ORG_PREDICATE)

                org_targets: list[str] = []
                if isinstance(org_raw, str) and org_raw:
                    org_targets = [org_raw]
                elif isinstance(org_raw, list):
                    org_targets = [t for t in org_raw if isinstance(t, str) and t]

                for org_cid in org_targets:
                    org_cid = org_cid if org_cid.startswith("#V#") else f"#V#{org_cid}"
                    org_slug = org_cid[3:] if org_cid.startswith("#V#") else org_cid
                    org_slug = org_slug.strip().lower().replace(" ", "_")
                    try:
                        role = get_user_role(user_slug, org_slug)
                    except Exception:
                        role = "member"

                    organisations.append(
                        {
                            "concept_id": org_cid,
                            "name": _prettify_concept_id(org_cid),
                            "role": role,
                        }
                    )

        # Fallback: stub role resolver mappings (Phase 1 hardcoded)
        if not organisations:
            org_roles = get_all_user_organisations(user_slug)
            for org_id, role in org_roles.items():
                concept_id = org_id if org_id.startswith("#V#") else f"#V#{org_id}"
                organisations.append(
                    {
                        "concept_id": concept_id,
                        "name": _prettify_concept_id(concept_id),
                        "role": role,
                    }
                )

        return (
            jsonify(
                {"organisations": organisations, "total_count": len(organisations)}
            ),
            200,
        )

    except Exception as e:
        print(f"Error getting organisations: {e}")
        return jsonify({"error": str(e)}), 500
