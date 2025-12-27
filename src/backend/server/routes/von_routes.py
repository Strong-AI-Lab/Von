from flask import Blueprint, request, jsonify, render_template, current_app, session
import os
import uuid
from datetime import datetime, timezone
from workflows.onboarding_workflow import run_onboarding_workflow  # fixed import path
from ...languagemodels.llm_interface import get_llm_client, get_active_model_name
from .settings_routes import get_all_settings_data
from ...integrations.internal_mcp import ToolCallParsingError
from ...services import chat_history_service

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

    # Remove duplicates while preserving order
    seen = set()
    unique_warnings = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            unique_warnings.append(w)

    return unique_warnings


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
def generate():
    """Handle text generation requests."""
    data = request.get_json()
    prompt_text = data.get("prompt", "")

    interaction_timestamp_utc = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    # Get user/org context from request body (sent by frontend from localStorage)
    request_user_id = data.get("user_id")
    request_org_id = data.get("org_id")
    request_language = data.get("language", "en-NZ")
    request_gmail_profile = data.get("gmail_profile")

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
        user_prompt_debug = {
            "effective_user_concept_id": user_concept_id,
            "loaded": False,
            "chars": 0,
            "prompt_concept_ids": [],
        }
        if user_concept_id:
            try:
                from ...services.chat_auxiliary_prompt_service import (
                    get_user_specific_prompt_fragments,
                )

                prompt_fragments = get_user_specific_prompt_fragments(user_concept_id)
                user_prompt_debug["prompt_concept_ids"] = [
                    frag.get("concept_id")
                    for frag in prompt_fragments
                    if isinstance(frag, dict)
                    and isinstance(frag.get("concept_id"), str)
                ]
                prompt_texts = []
                for frag in prompt_fragments:
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

        # ---------------------------------------------------------
        # Deterministic prompt introspection (avoid LLM self-report)
        # ---------------------------------------------------------
        deterministic_introspection_enabled = False
        try:
            import os

            flag_value = os.getenv("VON_DETERMINISTIC_INTROSPECTION", "0")
            deterministic_introspection_enabled = str(flag_value).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        except Exception:
            deterministic_introspection_enabled = False

        if (
            deterministic_introspection_enabled
            and user_concept_id
            and _is_prompt_introspection_question(prompt_text)
        ):
            gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
            import json as _json

            tool_messages = []
            tool_invocations = []

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

            current_turn_messages = [
                {"role": "user", "content": prompt_text}
            ] + tool_messages
            context_stats = _calculate_context_stats(context)
            current_context_stats = _calculate_context_stats(
                current_app.config["CONTEXT"]
            )
            tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None

            llm_debug_info = {
                "interaction_timestamp_utc": interaction_timestamp_utc,
                "model": model_name,
                "messages": current_turn_messages,
                "response": response_text,
                "user_prompt": user_prompt_debug,
                "context_stats": {
                    "sent_to_llm": context_stats,
                    "stored_context": current_context_stats,
                },
                "tool_stats": tool_stats,
                "tool_invocations": tool_invocations,
                "prompt_introspection_fastpath": {
                    "used_tool": used_tool,
                    "enabled": True,
                },
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

        # ---------------------------------------------------------
        # Deterministic tool inventory (avoid LLM narration)
        # ---------------------------------------------------------
        if deterministic_introspection_enabled and _is_tool_introspection_question(
            prompt_text
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
                    response_text = f"Could not retrieve tool inventory from the internal gateway: {exc}"
            else:
                response_text = (
                    "Internal MCP gateway is disabled; tool inventory is unavailable."
                )

            current_turn_messages = [{"role": "user", "content": prompt_text}]
            context_stats = _calculate_context_stats(context)
            current_context_stats = _calculate_context_stats(
                current_app.config["CONTEXT"]
            )

            llm_debug_info = {
                "interaction_timestamp_utc": interaction_timestamp_utc,
                "model": model_name,
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

            # Persist minimal history for continuity.
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
                current_app.config["CONTEXT"].append(
                    {"role": "user", "content": prompt_text}
                )
                current_app.config["CONTEXT"].append(
                    {"role": "assistant", "content": response_text}
                )
            current_app.config["CONTEXT"] = _limit_context_size(
                current_app.config["CONTEXT"], max_messages=20
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

        # ---------------------------------------------------------
        # Deterministic RAG status (avoid LLM narration)
        # ---------------------------------------------------------
        if (
            deterministic_introspection_enabled
            and user_concept_id
            and _is_rag_status_question(prompt_text)
        ):
            gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
            import json as _json

            tool_messages = []
            tool_invocations = []
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
                    response_text = (
                        f"I could not retrieve RAG status via internal tools: {exc}"
                    )
            else:
                response_text = (
                    "Internal MCP gateway is disabled; RAG status is unavailable."
                )

            # Persist messages in history/context.
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

            current_turn_messages = [
                {"role": "user", "content": prompt_text}
            ] + tool_messages
            context_stats = _calculate_context_stats(context)
            current_context_stats = _calculate_context_stats(
                current_app.config["CONTEXT"]
            )
            tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None

            llm_debug_info = {
                "interaction_timestamp_utc": interaction_timestamp_utc,
                "model": model_name,
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
        if user_concept_id:
            session_namespace = session.get("namespace")
            if (
                isinstance(session_namespace, str)
                and session_namespace.strip()
                and session_namespace.startswith("#V#")
            ):
                user_namespace = session_namespace.strip()
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
                else:
                    # Normalize to namespace format
                    user_id_normalized = (
                        user_concept_id.lower()
                        .replace(" ", "_")
                        .replace("#v#", "")
                        .replace("#", "")
                    )
                    user_namespace = f"#V#{user_id_normalized}"
                current_app.logger.info(
                    "[NAMESPACE] Derived user_namespace=%s from user_concept_id=%s",
                    user_namespace,
                    user_concept_id,
                )
        else:
            current_app.logger.warning(
                "[NAMESPACE] No user_concept_id - user_namespace=None (RAG unavailable)"
            )

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
                        "messages": current_turn_messages,
                        "response": response_text,
                        "user_prompt": user_prompt_debug,
                        "context_stats": {
                            "sent_to_llm": context_stats,
                            "stored_context": current_context_stats,
                        },
                        "tool_stats": tool_stats,
                        "tool_invocations": tool_invocations,
                        "aux_llm_calls": [],
                    }

                    return jsonify(
                        {
                            "response": response_text,
                            "llm_debug": llm_debug_info,
                        }
                    )

        auxiliary_llm_calls: list[dict] = []
        if orchestrator is None:
            response_text = llm_client.generate(
                prompt_text, context=enhanced_context, model=model_name
            )
            tool_invocations = []
        else:
            try:
                current_app.logger.info(
                    "[NAMESPACE] Calling orchestrator.run() with user_namespace=%s",
                    user_namespace,
                )
                orchestrator_result = orchestrator.run(
                    prompt=prompt_text,
                    context=enhanced_context,
                    llm_client=llm_client,
                    model=model_name,
                    user_namespace=user_namespace,
                    gmail_profile=request_gmail_profile,
                    auxiliary_system_prompt=auxiliary_system_prompt,
                )
                response_text = orchestrator_result.response_text
                tool_messages = [
                    dict(msg) for msg in orchestrator_result.extra_messages
                ]
                tool_invocations = list(orchestrator_result.tool_invocations)
                auxiliary_llm_calls = list(
                    getattr(orchestrator_result, "aux_llm_calls", [])
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

        llm_debug_info = {
            "interaction_timestamp_utc": interaction_timestamp_utc,
            "model": model_name,
            "messages": current_turn_messages,
            "response": response_text,
            "user_prompt": user_prompt_debug,
            "internal_mcp": {
                "gateway_present": gateway is not None,
                "gateway_enabled": (
                    bool(getattr(gateway, "enabled", False))
                    if gateway is not None
                    else False
                ),
                "orchestrator_present": orchestrator is not None,
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

        return jsonify({"response": response_text, "llm_debug": llm_debug_info})
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

        error_debug_info = {
            "interaction_timestamp_utc": interaction_timestamp_utc,
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
        return jsonify({"error": str(e), "llm_debug": error_debug_info}), 500


@von_bp.route("/history", methods=["GET"])
def history():
    """Retrieve chat history segments for the current user."""
    # Try to get user_id from session first, then query param
    user_concept_id = session.get("user_concept_id") or request.args.get("user_id")

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

    try:
        segments = chat_history_service.get_chat_history_segments(
            user_concept_id, session_id
        )
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

        has_more = segment_count < total_segments

        return jsonify(
            {
                "history": flattened_history,
                "segments_returned": segment_count,
                "total_segments": total_segments,
                "has_more_history": has_more,
            }
        )
    except Exception as e:
        print(f"Error retrieving history: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/history/length", methods=["GET"])
def history_length():
    """Retrieve the chat history length for the current user."""
    user_concept_id = session.get("user_concept_id")

    if not user_concept_id:
        return jsonify({"history_length": 0, "authenticated": False})

    try:
        length = chat_history_service.get_chat_history_length(user_concept_id)
        session_count = chat_history_service.get_chat_history_session_count(
            user_concept_id
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
    try:
        sessions = chat_history_service.get_chat_history_session_summaries(
            user_concept_id, limit=limit
        )
        return jsonify({"authenticated": True, "sessions": sessions})
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
            session.get("user_id")
            or session.get("user_concept_id")
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
            user_slug = user_slug.split("@")[0]
        user_slug = user_slug.strip().lower().replace(" ", "_")

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
            session.get("user_id")
            or session.get("user_concept_id")
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
                user_slug = user_slug.split("@")[0]
            user_slug = user_slug.strip().lower().replace(" ", "_")
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

        return (
            jsonify(
                {
                    "authenticated": True,
                    "user_id": user_id,
                    "organisation_id": org_id,
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

    Request body: {session_id: str}
    Returns: {status, session_id, history}

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

        # Verify the session belongs to this user.
        coll = chat_history_service.get_chat_history_collection_service()
        if coll is None:
            return jsonify({"error": "Chat history unavailable"}), 503

        doc = coll.find_one(
            {"user_id": user_concept_id, "session_id": session_id},
            {"history": 1},
        )
        if not doc:
            return jsonify({"error": "Session not found"}), 404

        history = doc.get("history") or []
        if not isinstance(history, list):
            history = []

        def _normalise_timestamp(value):
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.isoformat().replace("+00:00", "Z")
            return value

        normalised_history = []
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
                    "history": normalised_history,
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error setting chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/organisations/my_organisations", methods=["GET"])
def get_my_organisations():
    """
    Get list of organisations the user is member of.

    Returns: {organisations: [{concept_id, name, role}, ...], total_count}
    """
    try:
        from ...security.role_resolver import get_all_user_organisations

        user_id = (
            session.get("user_id")
            or session.get("user_concept_id")
            or session.get("user_email")
        )
        if not user_id:
            return jsonify({"error": "Not authenticated"}), 401

        # Derive a slug for stub role resolution
        user_slug = str(user_id)
        if user_slug.startswith("#V#"):
            user_slug = user_slug[3:]
        if "@" in user_slug:
            user_slug = user_slug.split("@")[0]
        user_slug = user_slug.strip().lower().replace(" ", "_")

        # Get orgs from role resolver (Phase 1 hardcoded mappings)
        # Returns dict: {org_id: role_name}
        org_roles = get_all_user_organisations(user_slug)

        # TODO: Once organisation concepts exist in Vontology, fetch their names
        # For now, use concept_id as name
        organisations = []
        for org_id, role in org_roles.items():
            concept_id = org_id if org_id.startswith("#V#") else f"#V#{org_id}"
            organisations.append(
                {
                    "concept_id": concept_id,
                    "name": concept_id.replace("#V#", "").replace("_", " ").title(),
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
