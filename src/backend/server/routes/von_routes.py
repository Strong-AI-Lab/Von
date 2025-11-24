from flask import Blueprint, request, jsonify, render_template, current_app, session
import os
import uuid
from workflows.onboarding_workflow import run_onboarding_workflow  # fixed import path
from ...languagemodels.llm_interface import get_llm_client, get_active_model_name
from .settings_routes import get_all_settings_data
from ...integrations.internal_mcp import ToolCallParsingError
from ...services import chat_history_service

# NOTE: Previous relative template_folder path ('../../frontend/...') was incorrect.
# From this file (src/backend/server/routes/von_routes.py) we need to traverse up THREE levels
# to reach the 'src' directory, then descend into frontend/web/von_interface/templates
# would raise TemplateNotFound for 'von_interface.html'.
_TEMPLATE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../frontend/web/von_interface/templates"))
von_bp = Blueprint('von', __name__, template_folder=_TEMPLATE_DIR)


def _truncate_large_tool_results(messages: list[dict], max_tool_content_chars: int = 5000) -> list[dict]:
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
                truncated_content = content[:max_tool_content_chars] + f"\n... [truncated {len(content) - max_tool_content_chars} chars]"
                result.append({**msg, "content": truncated_content})
            else:
                result.append(msg)
        else:
            result.append(msg)
    return result


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
        recent_msgs = context[-(max_messages-1):]  # Keep room for system message
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
        "largest_message": {"role": None, "chars": 0}
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
        "tools": []
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
            parsed = json.loads(content_str.split("[truncated")[0] if was_truncated else content_str)
            if isinstance(parsed, dict):
                tool_name = parsed.get("tool", "unknown")
        except:
            pass

        stats["tools"].append({
            "name": tool_name,
            "chars": content_len,
            "truncated": was_truncated
        })

    return stats
@von_bp.route("/onboard_new_member", methods=["POST"])
def onboard_new_member():
    """Onboards a new lab member."""
    data = request.get_json()
    member_name = data.get("member_name")

    if not member_name:
        return jsonify({"error": "No member name provided."}), 400

    try:
        run_onboarding_workflow(member_name)
        return jsonify({"message": f"Onboarding workflow started for {member_name}."}), 200
    except Exception as e:
        print(f"Error during onboarding: {e}") # Add server-side logging
        return jsonify({"error": f"Error during onboarding: {str(e)}"}), 500

@von_bp.route("/update_model", methods=["POST"])
def update_model():
    """Update the model based on user input."""
    data = request.get_json()
    new_model = data.get("model")

    if new_model:
        current_app.config["MODEL"] = new_model
        print(f"Model updated to: {new_model}") # Add server-side logging
        return jsonify({"message": f"Model updated to {new_model}"}), 200
    else:
        return jsonify({"error": "No model provided."}), 400

@von_bp.route("/")
def serve_page():
    """Serve the main chat interface (HTML file)."""
    try:
        # Ensure template changes (e.g., recent fixes) are picked up even in production mode
        jenv = getattr(current_app, 'jinja_env', None)
        cache = getattr(jenv, 'cache', None)
        clear_fn = getattr(cache, 'clear', None)
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

    # Get user/org context from request body (sent by frontend from localStorage)
    request_user_id = data.get("user_id")
    request_org_id = data.get("org_id")
    request_language = data.get("language", "en-NZ")

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
        org_concept_id = session.get('organisation_concept_id') if session else None

        # Store user_concept_id in session for history tracking
        if user_concept_id:
            session["user_concept_id"] = user_concept_id
    except Exception:
        user_concept_id = None
        org_concept_id = None

    try:
        llm_client = get_llm_client(user_concept_id=user_concept_id, org_concept_id=org_concept_id)
        model_name = get_active_model_name()
    except Exception as e:
        return jsonify({"error": f"Could not get LLM client: {e}"}), 500

    if not prompt_text:
        return jsonify({"error": "No prompt provided."}), 400

    try:
        # Build system message with user and organization context from request
        system_message_parts = []

        # Try to get user name from concept if user_id provided
        if request_user_id:
            try:
                from ...services.concept_service import get_concept_by_concept_id
                user_concept = get_concept_by_concept_id(request_user_id)
                if user_concept:
                    user_name = user_concept.get('name') or request_user_id
                    system_message_parts.append(f"Current user: {user_name} ({request_user_id})")
                    current_app.logger.info(f"User context: {user_name} ({request_user_id})")
                else:
                    system_message_parts.append(f"Current user ID: {request_user_id}")
            except Exception as e:
                current_app.logger.warning(f"Could not fetch user concept {request_user_id}: {e}")
                system_message_parts.append(f"Current user ID: {request_user_id}")

        # Try to get organization name from concept if org_id provided
        if request_org_id:
            try:
                from ...services.concept_service import get_concept_by_concept_id
                org_concept = get_concept_by_concept_id(request_org_id)
                if org_concept:
                    org_name = org_concept.get('name') or request_org_id
                    system_message_parts.append(f"Organization: {org_name} ({request_org_id})")
                    current_app.logger.info(f"Organization context: {org_name} ({request_org_id})")
                else:
                    system_message_parts.append(f"Organization ID: {request_org_id}")
            except Exception as e:
                current_app.logger.warning(f"Could not fetch org concept {request_org_id}: {e}")
                system_message_parts.append(f"Organization ID: {request_org_id}")

        # Add language preference if provided
        if request_language and request_language != "en-NZ":
            system_message_parts.append(f"Language preference: {request_language}")

        # Create enhanced context with system message if we have user/org info
        enhanced_context = context.copy()
        if system_message_parts:
            # Create system message
            system_message = "You are Von, an AI assistant. " + " | ".join(system_message_parts)

            # Insert system message at the beginning if not already present
            if not enhanced_context or enhanced_context[0].get("role") != "system":
                enhanced_context.insert(0, {"role": "system", "content": system_message})
            else:
                # Update existing system message to include user/org context
                existing_system = enhanced_context[0]["content"]
                if not any(part in existing_system for part in system_message_parts):
                    enhanced_context[0]["content"] = f"{existing_system} | {' | '.join(system_message_parts)}"

        # Log the enhanced context being sent to the model for debugging
        current_app.logger.info(f"Enhanced context being sent to model: {len(enhanced_context)} messages")
        for i, msg in enumerate(enhanced_context):
            current_app.logger.info(f"Message {i}: role={msg.get('role')}, content_preview={msg.get('content', '')[:100]}...")

        orchestrator = current_app.config.get("INTERNAL_MCP_ORCHESTRATOR")
        tool_messages: list[dict[str, str]] = []
        if orchestrator is None:
            response_text = llm_client.generate(prompt_text, context=enhanced_context, model=model_name)
            tool_invocations = []
        else:
            try:
                orchestrator_result = orchestrator.run(
                    prompt=prompt_text,
                    context=enhanced_context,
                    llm_client=llm_client,
                    model=model_name,
                )
                response_text = orchestrator_result.response_text
                tool_messages = [dict(msg) for msg in orchestrator_result.extra_messages]
                tool_invocations = list(orchestrator_result.tool_invocations)
            except ToolCallParsingError as exc:
                current_app.logger.warning("[mcp_orchestrator] Invalid tool request payload: %s", exc)
                response_text = llm_client.generate(prompt_text, context=enhanced_context, model=model_name)
                tool_invocations = []

        if tool_invocations:
            current_app.logger.info("[mcp_orchestrator] Tool invocations: %s", tool_invocations)

        # Store both user message and response in context AFTER calling the LLM
        # Truncate large tool results to prevent context explosion
        truncated_tool_messages = _truncate_large_tool_results(tool_messages, max_tool_content_chars=5000)

        if user_concept_id:
            chat_history_service.add_message_to_history(user_concept_id, session_id, {"role": "user", "content": prompt_text})
            for tool_msg in truncated_tool_messages:
                chat_history_service.add_message_to_history(user_concept_id, session_id, tool_msg)
            chat_history_service.add_message_to_history(user_concept_id, session_id, {"role": "assistant", "content": response_text})
        else:
            current_app.config["CONTEXT"].append({"role": "user", "content": prompt_text})
            for tool_msg in truncated_tool_messages:
                current_app.config["CONTEXT"].append(tool_msg)
            current_app.config["CONTEXT"].append({"role": "assistant", "content": response_text})


        # Limit overall context size to prevent unbounded growth
        current_app.config["CONTEXT"] = _limit_context_size(current_app.config["CONTEXT"], max_messages=20)

        # Build LLM debug information
        # NOTE: Only include the NEW messages for this turn to avoid exponential token growth
        # as the full context would include all previous turns' debug data
        current_turn_messages = [{"role": "user", "content": prompt_text}]
        if tool_messages:
            current_turn_messages.extend(tool_messages)

        # Calculate context statistics for visibility
        context_stats = _calculate_context_stats(enhanced_context)
        current_context_stats = _calculate_context_stats(current_app.config["CONTEXT"])

        # Calculate tool statistics if tools were used
        tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None

        llm_debug_info = {
            "model": model_name,
            "messages": current_turn_messages,
            "response": response_text,
            "context_stats": {
                "sent_to_llm": context_stats,  # What was actually sent this turn
                "stored_context": current_context_stats  # Current state after this turn
            },
            "tool_stats": tool_stats,  # MCP tool result statistics
            "tool_invocations": [
                {
                    "method": inv.get("tool") or inv.get("method", "unknown"),
                    "arguments": inv.get("payload") or inv.get("arguments", {}),
                    "error": inv.get("error")
                }
                for inv in tool_invocations
            ] if tool_invocations else []
        }

        return jsonify({
            "response": response_text,
            "llm_debug": llm_debug_info
        })
    except Exception as e:
        print(f"Error during generation: {e}") # Log error server-side
        # Return error with debug info showing the current turn only (not full context)
        # to avoid exponential token growth in debug data

        # Calculate context stats if available
        context_stats = None
        if 'enhanced_context' in locals():
            try:
                context_stats = {
                    "sent_to_llm": _calculate_context_stats(enhanced_context)
                }
            except:
                pass

        error_debug_info = {
            "model": model_name if 'model_name' in locals() else 'unknown',
            "messages": [{"role": "user", "content": prompt_text}],
            "response": None,
            "error": str(e),
            "context_stats": context_stats,
            "tool_invocations": []
        }
        return jsonify({"error": str(e), "llm_debug": error_debug_info}), 500

@von_bp.route("/history", methods=["GET"])
def history():
    """Retrieve chat history segments for the current user."""
    user_concept_id = session.get("user_concept_id")
    session_id = session.get("session_id")

    if not user_concept_id or not session_id:
        return jsonify({
            "history": [],
            "segments_returned": 0,
            "total_segments": 0,
            "has_more_history": False
        })

    requested_segments = request.args.get("segments", default=1, type=int)
    segment_count = max(1, requested_segments)

    try:
        segments = chat_history_service.get_chat_history_segments(user_concept_id, session_id)
        total_segments = len(segments)

        if total_segments == 0:
            return jsonify({
                "history": [],
                "segments_returned": 0,
                "total_segments": 0,
                "has_more_history": False
            })

        segment_count = min(segment_count, total_segments)
        selected_segments = segments[-segment_count:]
        flattened_history = [msg for segment in selected_segments for msg in segment]

        has_more = segment_count < total_segments

        return jsonify({
            "history": flattened_history,
            "segments_returned": segment_count,
            "total_segments": total_segments,
            "has_more_history": has_more
        })
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
        return jsonify({"history_length": length, "authenticated": True})
    except Exception as e:
        print(f"Error retrieving history length: {e}")
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
        print(f"Error fetching models: {e}") # Log error server-side
        return jsonify({"error": str(e)}), 500

@von_bp.route("/reset", methods=["POST"])
def reset_context():
    """Reset the conversation context."""
    try:
        user_concept_id = session.get("user_concept_id")
        session_id = session.get("session_id")

        if user_concept_id and session_id:
            chat_history_service.add_reset_marker_to_history(user_concept_id, session_id)

        # Clear the conversation context
        current_app.config["CONTEXT"] = []
        print("Context reset successfully") # Log server-side
        return jsonify({"status": "reset", "message": "Context reset successfully"}), 200
    except Exception as e:
        print(f"Error resetting context: {e}") # Log error server-side
        return jsonify({"error": f"Failed to reset context: {str(e)}"}), 500
