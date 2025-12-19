"""Chat assistant orchestration utilities for the internal MCP gateway."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence

from .gateway import InternalMCPGateway


@dataclass(frozen=True)
class OrchestratorResult:
    """Structured response from a tool-aware chat exchange."""

    response_text: str
    extra_messages: Sequence[Mapping[str, Any]]
    tool_invocations: Sequence[Mapping[str, Any]]


class ToolCallParsingError(Exception):
    """Raised when a model returns an invalid tool call payload."""


class InternalMCPChatOrchestrator:
    """Simple loop that allows the Von assistant to call internal tools."""

    _ACTION_FIELD = "action"
    _CALL_ACTION = "call_tool"
    _TOOL_FIELD = "tool"
    _PAYLOAD_FIELD = "payload"

    def __init__(
        self,
        *,
        gateway: InternalMCPGateway,
        logger: logging.Logger | None = None,
        max_tool_invocations: int = 1,
        default_gmail_profile: str | None = None,
        max_context_chars: int | None = None,
        max_tool_result_chars: int | None = None,
        max_tool_result_field_chars: int | None = None,
    ) -> None:
        self._gateway = gateway
        self._logger = logger or logging.getLogger(__name__)
        self._max_tool_invocations = max(0, int(max_tool_invocations))
        self._default_gmail_profile = default_gmail_profile

        # Guardrails against context/tool-result bloat.
        # These are expressed in characters (not tokens) to avoid model-specific tokenisers.
        self._max_context_chars = self._coerce_int(
            max_context_chars,
            env_var="VON_MCP_MAX_CONTEXT_CHARS",
            default=120_000,
            min_value=4_000,
            max_value=2_000_000,
        )
        self._max_tool_result_chars = self._coerce_int(
            max_tool_result_chars,
            env_var="VON_MCP_MAX_TOOL_RESULT_CHARS",
            default=20_000,
            min_value=2_000,
            max_value=1_000_000,
        )
        self._max_tool_result_field_chars = self._coerce_int(
            max_tool_result_field_chars,
            env_var="VON_MCP_MAX_TOOL_RESULT_FIELD_CHARS",
            default=8_000,
            min_value=1_000,
            max_value=200_000,
        )

    @staticmethod
    def _coerce_int(
        value: int | None,
        *,
        env_var: str,
        default: int,
        min_value: int,
        max_value: int,
    ) -> int:
        if value is None:
            raw = os.environ.get(env_var)
            if raw is not None:
                try:
                    value = int(raw)
                except Exception:
                    value = None
        if value is None:
            value = default
        return max(min_value, min(max_value, int(value)))

    def _limit_context_for_llm(self, messages: List[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
        """Limit context size to avoid runaway prompt growth.

        Keeps the system message (index 0) and then includes as many of the most
        recent remaining messages as fit under the max-context budget.
        """
        if not messages:
            return []

        system_msg = messages[0]
        rest = list(messages[1:])

        def _msg_len(msg: Mapping[str, Any]) -> int:
            content = msg.get("content")
            if isinstance(content, str):
                return len(content)
            return len(str(content))

        budget = self._max_context_chars
        # Always keep system message; it is essential for tool behaviour.
        total = _msg_len(system_msg)

        kept_reversed: List[Mapping[str, Any]] = []
        for msg in reversed(rest):
            msg_len = _msg_len(msg)
            if kept_reversed and (total + msg_len) > budget:
                break
            # Always include at least the most recent message even if it exceeds budget.
            if not kept_reversed and (total + msg_len) > budget:
                kept_reversed.append(msg)
                total += msg_len
                break
            kept_reversed.append(msg)
            total += msg_len

        kept = list(reversed(kept_reversed))
        trimmed = [system_msg, *kept]

        if len(trimmed) < len(messages):
            self._logger.info(
                "[mcp_orchestrator] Trimmed context from %d to %d messages (max_context_chars=%d).",
                len(messages),
                len(trimmed),
                self._max_context_chars,
            )
        return trimmed

    def _truncate_nested_for_llm(self, value: Any, *, max_string_chars: int, max_list_items: int = 50) -> Any:
        """Truncate nested tool payloads so they remain useful but bounded."""
        if isinstance(value, str):
            if len(value) <= max_string_chars:
                return value
            return value[:max_string_chars] + f"\n... [truncated {len(value) - max_string_chars} chars]"
        if isinstance(value, list):
            if len(value) <= max_list_items:
                return [self._truncate_nested_for_llm(v, max_string_chars=max_string_chars, max_list_items=max_list_items) for v in value]
            head = value[: max_list_items - 1]
            tail = value[-1:]
            truncated = [
                *[self._truncate_nested_for_llm(v, max_string_chars=max_string_chars, max_list_items=max_list_items) for v in head],
                {"_truncated": True, "_omitted_items": len(value) - len(head) - len(tail)},
                *[self._truncate_nested_for_llm(v, max_string_chars=max_string_chars, max_list_items=max_list_items) for v in tail],
            ]
            return truncated
        if isinstance(value, dict):
            return {
                str(k): self._truncate_nested_for_llm(v, max_string_chars=max_string_chars, max_list_items=max_list_items)
                for k, v in value.items()
            }
        return value

    def _tool_listing(self) -> str:
        """Generate a concise listing of available tools for the system prompt.

        Keep this as short as possible to avoid token bloat. Only include
        essential information: tool name, brief description, and key parameters.
        """
        catalogue = self._gateway.describe_methods()
        lines: List[str] = []
        for name in sorted(catalogue.keys()):
            metadata = catalogue[name]
            if not isinstance(metadata, Mapping):
                continue
            description = metadata.get("description") or ""
            # Truncate long descriptions (increased to 200 for param examples)
            if len(description) > 200:
                description = description[:197] + "..."

            input_schema = metadata.get("input_schema") or {}
            required = input_schema.get("required") or []
            optional = input_schema.get("optional") or {}

            # Show required params + important optional params
            params_list = list(required)
            # For concept search alias, always show instance_of since it's critical
            if name == "vontology_concept_search" and "instance_of" in optional:
                params_list.append("instance_of (optional)")

            if params_list:
                params = f" (params: {', '.join(params_list)})"
            else:
                params = ""

            lines.append(f"- {name}{params}: {description}")
        return "\n".join(lines)

    def _instruction_message(
        self,
        user_namespace: str | None = None,
        auxiliary_system_prompt: str | None = None,
    ) -> str:
        """Build system instruction emphasizing immediate tool invocation behavior.

        Design rationale (JVNAUTOSCI-698): Focus on BEHAVIOR (invoke immediately)
        rather than FORMAT (JSON structure) to prevent LLMs from outputting JSON as
        a description of intent rather than triggering actual execution.
        """
        listing = self._tool_listing()

        # Inform agent about authentication status and tool availability
        auth_status = ""
        if user_namespace:
            auth_status = f"\n\n🔐 AUTHENTICATION STATUS: Authenticated (namespace: {user_namespace})\nRAG tools (search_knowledge_base, rag_list_indexed, rag_get_item) are AVAILABLE.\n"
        else:
            auth_status = (
                "\n\n⚠️ AUTHENTICATION STATUS: NOT AUTHENTICATED\n"
                "RAG tools (search_knowledge_base, rag_list_indexed, rag_get_item) are UNAVAILABLE.\n"
                "These tools require user authentication to prevent cross-user data access.\n"
                "If user asks about their RAG data/sessions/indexed content, explain they need to log in first.\n"
            )

        base_message = (
            "You have access to internal MCP tools.\n\n"
            "⚠️ WHEN TO USE TOOLS (CHECK THESE FIRST) ⚠️\n"
            "If user asks for RECENT, CURRENT, LATEST, NEW, or BREAKING information → USE search_web\n"
            "If user mentions specific dates (2024+, 2025+, \"this year\", \"this month\") → USE search_web\n"
            "If user explicitly says \"search\", \"look up\", \"find information on\" → USE search_web\n"
            "If user asks \"what's new\", \"recent developments\", \"latest research\" → USE search_web\n"
            "If user provides a URL to analyse or extract content from → USE extract_url (or resilient_extract_url for JS-heavy/blocked pages)\n"
            "If user asks a direct factual question needing verification → USE qna_search\n"
            "If searching within specific domain/context (e.g., site:example.com) → USE context_search\n"
            "If user asks about arXiv papers by author, topic, or ID → USE list_papers or read_paper\n"
            "If user asks about RAG sessions/conversations (\"how many\", \"what's indexed\", \"list sessions\") → USE rag_list_indexed\n"
            "If user wants to see RAG content from a specific session → USE rag_get_item\n"
            "If user wants to search their indexed conversations by topic/keyword → USE search_knowledge_base\n\n"
            "CRITICAL: Your training data has a cutoff date. For anything described as current/recent/new, "
            "you MUST use search tools to get up-to-date information.\n\n"
            "HOW TO INVOKE A TOOL:\n"
            "Respond with EITHER a single tool-call JSON object OR a JSON array of tool-call objects:\n"
            "Single:\n"
            "{\"action\": \"call_tool\", \"tool\": \"tool_name\", \"payload\": {\"param\": \"value\"}}\n"
            "Batch (preferred for multi-step workflows; keep it small):\n"
            "[{\"action\": \"call_tool\", \"tool\": \"tool_a\", \"payload\": {}}, {\"action\": \"call_tool\", \"tool\": \"tool_b\", \"payload\": {}}]\n\n"
            "INVOCATION RULES:\n"
            "- DO NOT explain what you're going to do - just do it\n"
            "- DO NOT output JSON as an example or description - only output JSON when you want to invoke a tool NOW\n"
            "- DO NOT say 'I will call' or 'Let me call' - just call it\n"
            "- You MAY batch multiple tool calls in ONE message as a JSON array (keep it to <= 4 calls)\n"
            "- After you receive the tool result (role 'tool'), respond naturally to the user\n\n"
            "If you output JSON, the system will execute that tool call immediately.\n\n"
            "IMPORTANT: arXiv paper conversions (PDF to markdown) can take 5-10 minutes.\n"
            "If read_paper fails, the paper may still be converting. Check with list_papers.\n\n"
            "Available tools:\n"
            f"{listing}"
        )

        if auxiliary_system_prompt and isinstance(auxiliary_system_prompt, str) and auxiliary_system_prompt.strip():
            base_message += (
                "\n\n"
                "USER-SPECIFIC SYSTEM PROMPT (from Vontology):\n"
                f"{auxiliary_system_prompt.strip()}\n"
            )

        return base_message

    @staticmethod
    def _looks_like_missing_tool_call(response: str) -> bool:
        """Heuristic: model appears to promise a tool-backed action but emitted no tool-call JSON.

        This protects against a common failure mode where the model says e.g.
        "Here is the actual ontology operation" and then stops, resulting in a
        silent no-op.

        Keep this conservative to avoid forcing tool calls for normal prose.
        """

        if not isinstance(response, str):
            return False
        text = response.strip()
        if not text:
            return False

        lowered = text.lower()
        triggers = (
            "here is the actual ontology operation",
            "here's the actual ontology operation",
            "here is the actual tool call",
            "here's the actual tool call",
            "here is the actual ontology operation.",
            "here is the actual ontology operation:",
            "here is the actual operation",
            "here's the actual operation",
        )
        if any(t in lowered for t in triggers):
            return True

        # Secondary trigger: explicit step-by-step change description without any tool JSON.
        # Only count as missing-tool-call if the assistant also uses tool-y language.
        toolish = ("ontology", "mcp", "tool", "operation")
        if any(k in lowered for k in toolish):
            if "->" in text or "→" in text:
                return True

        return False

    def _is_json_action_response(self, response: str) -> bool:
        """Detect if response is ONLY a JSON action object (common LLM failure mode).

        Returns True if the response is a tool call that should have been executed
        but was returned as text instead. This detection helps identify when the
        instruction message needs refinement for specific models (JVNAUTOSCI-698).
        """
        stripped = response.strip()

        # Check if entire response is a single JSON object or a JSON array.
        if not (
            (stripped.startswith('{') and stripped.endswith('}'))
            or (stripped.startswith('[') and stripped.endswith(']'))
        ):
            return False

        # Check if it contains action/tool/payload structure
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                if not parsed:
                    return False
                return any(self._is_json_action_response(json.dumps(item)) for item in parsed)
            if not isinstance(parsed, dict):
                return False

            # Exact match for tool call pattern
            has_action = parsed.get(self._ACTION_FIELD) == self._CALL_ACTION
            has_tool = self._TOOL_FIELD in parsed and isinstance(parsed[self._TOOL_FIELD], str)
            has_payload = self._PAYLOAD_FIELD in parsed and isinstance(parsed[self._PAYLOAD_FIELD], dict)

            # Some models omit the action field even when they are clearly
            # attempting a tool call. Treat this as a likely tool call response
            # for diagnostics (execution is handled separately).
            missing_action_but_tool_shape = (
                self._ACTION_FIELD not in parsed
                and has_tool
                and has_payload
                and set(parsed.keys()) <= {self._TOOL_FIELD, self._PAYLOAD_FIELD}
            )

            return (has_action and has_tool and has_payload) or missing_action_but_tool_shape
        except (json.JSONDecodeError, TypeError):
            return False

    def _extract_tool_calls(self, text: str) -> Optional[List[MutableMapping[str, Any]]]:
        """Extract one or more tool-call objects from the model response.

        Accepts either a single tool-call JSON object or a JSON array of tool-call
        objects. Returns None if the response is not a pure tool call.

        This keeps the original safety constraints:
        - Tool calls must be the first JSON value in the response.
        - Concatenated JSON objects are rejected.
        - Missing action is only accepted for strict {tool, payload} shapes when
          the tool is known.
        """

        raw = text.strip()
        if not raw:
            return None

        post_fence_trailing: str = ""
        normalised = raw.replace("\r\n", "\n")

        fence_idx = normalised.find("```")
        if fence_idx != -1:
            prefix = normalised[:fence_idx]
            prefix_ok = (
                not prefix.strip()
                or (
                    len(prefix) <= 200
                    and prefix.count("\n") <= 2
                    and all(token not in prefix for token in ("{", "[", "}"))
                )
            )
            if prefix_ok:
                fenced = normalised[fence_idx:]
                if fenced.startswith("```"):
                    open_line_end = fenced.find("\n")
                    if open_line_end != -1:
                        close_marker = "\n```"
                        close_idx = fenced.find(close_marker, open_line_end + 1)
                        if close_idx != -1:
                            close_line_end = fenced.find("\n", close_idx + 1)
                            if close_line_end == -1:
                                close_line_end = len(fenced)
                            post_fence_trailing = fenced[close_line_end:].strip()
                            raw = fenced[open_line_end + 1 : close_idx].strip()

        looks_like_tool_call = any(token in raw for token in (f'"{self._ACTION_FIELD}"', f'"{self._TOOL_FIELD}"'))

        if not (raw.startswith("{") or raw.startswith("[")):
            return None

        decoder = json.JSONDecoder()
        try:
            parsed, end = decoder.raw_decode(raw)
        except json.JSONDecodeError as exc:
            raise ToolCallParsingError(
                "Tool call was not executed: invalid JSON in tool call response."
            ) from exc

        trailing = raw[end:].strip()

        def _normalise_tool_call(candidate: Any) -> Optional[MutableMapping[str, Any]]:
            if not isinstance(candidate, MutableMapping):
                return None

            has_tool = isinstance(candidate.get(self._TOOL_FIELD), str)
            has_payload = isinstance(candidate.get(self._PAYLOAD_FIELD, {}), MutableMapping)
            has_action = candidate.get(self._ACTION_FIELD) == self._CALL_ACTION

            missing_action = self._ACTION_FIELD not in candidate
            strict_shape = set(candidate.keys()) <= {self._TOOL_FIELD, self._PAYLOAD_FIELD}

            if missing_action and has_tool and has_payload and strict_shape:
                catalogue = None
                describe_methods = getattr(self._gateway, "describe_methods", None)
                if callable(describe_methods):
                    catalogue = describe_methods()
                tool_name = candidate.get(self._TOOL_FIELD)
                if isinstance(catalogue, Mapping) and tool_name in catalogue:
                    candidate[self._ACTION_FIELD] = self._CALL_ACTION
                    has_action = True
                else:
                    return None

            if not (has_action and has_tool and has_payload):
                return None

            return candidate

        tool_calls: List[MutableMapping[str, Any]] = []
        if isinstance(parsed, MutableMapping):
            tool_call = _normalise_tool_call(parsed)
            if tool_call is None:
                return None
            tool_calls = [tool_call]
        elif isinstance(parsed, list):
            for item in parsed:
                tool_call = _normalise_tool_call(item)
                if tool_call is None:
                    if looks_like_tool_call:
                        raise ToolCallParsingError(
                            "Tool call was not executed: tool call batch must be a JSON array of tool-call objects."
                        )
                    return None
                tool_calls.append(tool_call)
            if not tool_calls:
                return None
        else:
            if looks_like_tool_call:
                raise ToolCallParsingError(
                    "Tool call was not executed: tool call must be a JSON object or array."
                )
            return None

        # Reject concatenated JSON values.
        if trailing:
            if trailing.startswith("{") or trailing.startswith("["):
                raise ToolCallParsingError(
                    "Tool call was not executed: multiple JSON values were emitted in one response."
                )

            snippet = trailing
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            self._logger.warning(
                "[mcp_orchestrator] Stripping trailing non-JSON text after tool call (len=%d): %r",
                len(trailing),
                snippet,
            )

        if post_fence_trailing:
            if post_fence_trailing.startswith("{") or post_fence_trailing.startswith("["):
                raise ToolCallParsingError(
                    "Tool call was not executed: multiple JSON values were emitted in one response."
                )
            snippet = post_fence_trailing
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            self._logger.warning(
                "[mcp_orchestrator] Stripping trailing non-JSON text after fenced tool call (len=%d): %r",
                len(post_fence_trailing),
                snippet,
            )

        return tool_calls

    def _extract_json_blob(self, text: str) -> Optional[MutableMapping[str, Any]]:
        """Parse a single JSON object from the model response.

        Tool calls should be emitted as a *pure* JSON object with no surrounding
        text (including Markdown fences).

        In practice, some models occasionally append explanatory prose after an
        otherwise valid tool-call JSON object. We tolerate that trailing text as
        long as it does not begin another JSON value.

        This strictness prevents silent failures where malformed output (e.g.
        trailing characters like `}x`) or multiple JSON objects are treated as a
        valid tool call (JVNAUTOSCI-798).
        """
        raw = text.strip()
        if not raw:
            return None

        post_fence_trailing: str = ""
        normalised = raw.replace("\r\n", "\n")

        fence_idx = normalised.find("```")
        if fence_idx != -1:
            prefix = normalised[:fence_idx]
            # Some models add a short one-line preface before the fenced tool
            # call. Tolerate that as long as it is small and does not itself
            # contain JSON-like openers.
            prefix_ok = (
                not prefix.strip()
                or (
                    len(prefix) <= 200
                    and prefix.count("\n") <= 2
                    and all(token not in prefix for token in ("{", "[", "}"))
                )
            )
            if prefix_ok:
                fenced = normalised[fence_idx:]
                if fenced.startswith("```"):
                    open_line_end = fenced.find("\n")
                    if open_line_end != -1:
                        close_marker = "\n```"
                        close_idx = fenced.find(close_marker, open_line_end + 1)
                        if close_idx != -1:
                            close_line_end = fenced.find("\n", close_idx + 1)
                            if close_line_end == -1:
                                close_line_end = len(fenced)
                            post_fence_trailing = fenced[close_line_end:].strip()
                            raw = fenced[open_line_end + 1 : close_idx].strip()

        looks_like_tool_call = any(token in raw for token in (f'"{self._ACTION_FIELD}"', f'"{self._TOOL_FIELD}"'))

        # Only consider a tool call if the model output begins with a JSON
        # object (or a fenced block containing one). This avoids false
        # positives when the model discusses tool calls or the user has pasted
        # JSON in the conversation.
        if not raw.startswith("{"):
            return None

        decoder = json.JSONDecoder()
        try:
            parsed, end = decoder.raw_decode(raw)
        except json.JSONDecodeError as exc:
            raise ToolCallParsingError(
                "Tool call was not executed: invalid JSON in tool call response."
            ) from exc

        is_tool_call = False
        if isinstance(parsed, MutableMapping):
            has_tool = isinstance(parsed.get(self._TOOL_FIELD), str)
            has_payload = isinstance(parsed.get(self._PAYLOAD_FIELD, {}), MutableMapping)

            has_action = parsed.get(self._ACTION_FIELD) == self._CALL_ACTION

            # Backwards/robust parsing: accept missing action if the payload is
            # a strict tool-call shape ({tool, payload}) and the tool name is
            # recognised. This avoids treating arbitrary JSON responses as tool
            # calls.
            missing_action = self._ACTION_FIELD not in parsed
            strict_shape = set(parsed.keys()) <= {self._TOOL_FIELD, self._PAYLOAD_FIELD}

            if missing_action and has_tool and has_payload and strict_shape:
                catalogue = None
                describe_methods = getattr(self._gateway, "describe_methods", None)
                if callable(describe_methods):
                    catalogue = describe_methods()
                tool_name = parsed.get(self._TOOL_FIELD)
                if isinstance(catalogue, Mapping):
                    if tool_name in catalogue:
                        parsed[self._ACTION_FIELD] = self._CALL_ACTION
                        has_action = True
                    else:
                        # Treat unknown {tool, payload} JSON as a normal response.
                        # This prevents arbitrary JSON from being misclassified as
                        # a tool call (JVNAUTOSCI-798).
                        return None

            is_tool_call = has_action and has_tool and has_payload

        trailing = raw[end:].strip()
        if trailing and is_tool_call:
            if trailing.startswith("{") or trailing.startswith("["):
                raise ToolCallParsingError(
                    "Tool call was not executed: multiple tool calls were emitted in one response."
                )

            # Some models append explanatory prose after a valid JSON tool call.
            # Prefer a safe recovery that preserves the single-tool-call contract
            # while avoiding hard failures for harmless trailing text.
            snippet = trailing
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            self._logger.warning(
                "[mcp_orchestrator] Stripping trailing non-JSON text after tool call (len=%d): %r",
                len(trailing),
                snippet,
            )
            trailing = ""

        if post_fence_trailing and is_tool_call:
            if post_fence_trailing.startswith("{") or post_fence_trailing.startswith("["):
                raise ToolCallParsingError(
                    "Tool call was not executed: multiple tool calls were emitted in one response."
                )
            snippet = post_fence_trailing
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            self._logger.warning(
                "[mcp_orchestrator] Stripping trailing non-JSON text after fenced tool call (len=%d): %r",
                len(post_fence_trailing),
                snippet,
            )
            post_fence_trailing = ""

        if trailing:
            # If it's not a tool call, treat the message as a normal response.
            return None

        if post_fence_trailing:
            # If it's not a tool call, treat the message as a normal response.
            return None

        if not isinstance(parsed, MutableMapping):
            if looks_like_tool_call:
                raise ToolCallParsingError(
                    "Tool call was not executed: tool call must be a JSON object."
                )
            return None

        # Only surface parsed JSON when it is a valid tool call. This avoids
        # accidentally treating arbitrary JSON responses as tool requests.
        if not is_tool_call:
            return None

        return parsed

    def _format_tool_result(self, tool_name: str, payload: Any, duration_ms: float | None, status: str, error: str | None = None) -> str:
        result: Dict[str, Any] = {
            "tool": tool_name,
            "status": status,
            "duration_ms": duration_ms,
        }
        if status == "ok":
            # Tool outputs can be very large (e.g., web extraction). Truncate nested
            # strings/lists so the *next* model call doesn't balloon.
            result["payload"] = self._truncate_nested_for_llm(
                payload,
                max_string_chars=self._max_tool_result_field_chars,
            )
        if error is not None:
            result["error"] = error
        try:
            encoded = json.dumps(result, default=str)
            if len(encoded) <= self._max_tool_result_chars:
                return encoded

            # If still too large, replace payload with a preview.
            preview = result.get("payload")
            preview_str = json.dumps(preview, default=str)
            if len(preview_str) > self._max_tool_result_chars:
                preview_str = preview_str[: self._max_tool_result_chars] + "\n... [truncated]"
            result["payload"] = {
                "_truncated": True,
                "_original_chars": len(encoded),
                "_preview": preview_str,
            }
            return json.dumps(result, default=str)
        except Exception:
            # Fallback to manual coercion if payload is not JSON serialisable
            safe_payload = result.get("payload")
            if safe_payload is not None:
                result["payload"] = str(safe_payload)
            return json.dumps(result, default=str)

    def _build_augmented_context(
        self,
        context: Optional[Sequence[Mapping[str, Any]]],
        user_namespace: str | None = None,
        auxiliary_system_prompt: str | None = None,
    ) -> List[Mapping[str, Any]]:
        base: List[Mapping[str, Any]] = []
        if context:
            for msg in context:
                if isinstance(msg, Mapping):
                    base.append(dict(msg))
        instruction_msg = self._instruction_message(
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
        )

        # Log the size of the instruction message for diagnostics
        instruction_chars = len(instruction_msg)
        if instruction_chars > 10000:
            self._logger.warning(
                "[mcp_orchestrator] Large instruction message: %d chars (%d KB). "
                "This may cause token limit issues.",
                instruction_chars, instruction_chars // 1024
            )

        base.insert(0, {"role": "system", "content": instruction_msg})
        return self._limit_context_for_llm(base)

    def run(
        self,
        *,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]],
        llm_client: Any,
        model: Optional[str],
        user_namespace: Optional[str] = None,
        gmail_profile: Optional[str] = None,
        auxiliary_system_prompt: str | None = None,
    ) -> OrchestratorResult:
        if not self._gateway.enabled or self._max_tool_invocations <= 0:
            response = llm_client.generate(prompt, context=context, model=model)
            # If the model emitted a tool-call JSON blob but the gateway is disabled,
            # surface an actionable message instead of returning raw JSON.
            try:
                tool_calls = self._extract_tool_calls(response)
            except ToolCallParsingError:
                tool_calls = None
            if tool_calls:
                tool_name = tool_calls[0].get(self._TOOL_FIELD)
                if isinstance(tool_name, str):
                    return OrchestratorResult(
                        response_text=(
                            "Internal MCP is disabled, so I couldn't execute the tool call "
                            f"for tool={tool_name!r}. Set VON_INTERNAL_MCP_ENABLE=1 and try again."
                        ),
                        extra_messages=(),
                        tool_invocations=(),
                    )
            return OrchestratorResult(response_text=response, extra_messages=(), tool_invocations=())

        augmented_context = self._build_augmented_context(
            context,
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
        )
        response = llm_client.generate(prompt, context=augmented_context, model=model)

        # Detect JSON tool-call output for diagnostics (JVNAUTOSCI-698).
        # Only warn if we fail to parse/execute it.
        is_json_action = self._is_json_action_response(response)

        # Extract potential tool request(s)
        tool_calls = self._extract_tool_calls(response)
        has_valid_tool_call = bool(tool_calls)

        # Only attempt extraction if detection passed (has action="call_tool" structure)
        # This prevents treating tool result JSON as tool call requests (JVNAUTOSCI-699)
        if not has_valid_tool_call:
            if is_json_action:
                self._logger.warning(
                    "[mcp_orchestrator] Model emitted JSON tool-call output but it was not executed. "
                    "This indicates a tool-call schema mismatch or parsing issue for model: %s",
                    model or "default",
                )

            # Recovery: if the model strongly indicates it intended to perform a
            # tool-backed action but did not emit a tool call, ask once more for
            # the actual tool-call JSON.
            if self._looks_like_missing_tool_call(response):
                self._logger.info(
                    "[mcp_orchestrator] Model response looks like a missing tool call; retrying once (model=%s).",
                    model or "default",
                )
                retry_prompt = (
                    "Your previous message described an action that requires MCP tools, but you did not emit a tool call. "
                    "NOW respond with ONLY a tool-call JSON object or a JSON array of tool-call objects (no prose, no Markdown)."
                )
                retry_response = llm_client.generate(retry_prompt, context=augmented_context, model=model)
                try:
                    retry_calls = self._extract_tool_calls(retry_response)
                except ToolCallParsingError:
                    retry_calls = None
                if retry_calls:
                    response = retry_response
                    tool_calls = retry_calls
                    has_valid_tool_call = True
                else:
                    # Fall back to returning the original response.
                    return OrchestratorResult(response_text=response, extra_messages=(), tool_invocations=())
            else:
                return OrchestratorResult(response_text=response, extra_messages=(), tool_invocations=())

        assert tool_calls is not None
        self._logger.debug("[mcp_orchestrator] Extracted tool requests: %s", tool_calls)

        invocations: List[Mapping[str, Any]] = []
        tool_messages: List[Mapping[str, Any]] = []
        selected_gmail_profile = gmail_profile or self._default_gmail_profile

        # Support chained tool calls up to max_tool_invocations limit (JVNAUTOSCI-699)
        iteration_count = 0
        current_response = response

        while iteration_count < self._max_tool_invocations:
            tool_calls = self._extract_tool_calls(current_response)
            if not tool_calls:
                break

            # Enforce invocation limit across batched calls.
            remaining = self._max_tool_invocations - iteration_count
            if remaining <= 0:
                break
            batch_cap = 4
            allowed = min(remaining, batch_cap)
            if len(tool_calls) > allowed:
                tool_calls = tool_calls[:allowed]

            # Add the assistant JSON response once per batch.
            if iteration_count == 0:
                augmented_context.extend([
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": current_response},
                ])
            else:
                augmented_context.append({"role": "assistant", "content": current_response})

            for tool_request in tool_calls:
                iteration_count += 1
                tool_name = tool_request.get(self._TOOL_FIELD)
                payload = tool_request.get(self._PAYLOAD_FIELD) or {}

                if not isinstance(tool_name, str):
                    raise ToolCallParsingError("Tool name must be a string.")
                if not isinstance(payload, MutableMapping):
                    raise ToolCallParsingError("Tool payload must be a JSON object.")

                try:
                    if tool_name.startswith("gmail_"):
                        if not payload.get("profile") and selected_gmail_profile:
                            payload["profile"] = selected_gmail_profile
                            self._logger.info(
                                "[mcp_orchestrator] Injected gmail_profile=%s into tool=%s payload",
                                selected_gmail_profile,
                                tool_name,
                            )
                        elif not payload.get("profile"):
                            self._logger.warning(
                                "[mcp_orchestrator] Gmail tool=%s invoked without profile and no default configured",
                                tool_name,
                            )

                    # Inject user_namespace into payload for non-Gmail tools only; Gmail MCP rejects unexpected fields
                    if not tool_name.startswith("gmail_"):
                        if user_namespace and "namespace" not in payload:
                            payload["namespace"] = user_namespace
                            self._logger.info(
                                "[mcp_orchestrator] Injected namespace=%s into tool=%s payload",
                                user_namespace,
                                tool_name
                            )
                        elif user_namespace:
                            self._logger.info(
                                "[mcp_orchestrator] Tool=%s already has namespace=%s in payload",
                                tool_name,
                                payload.get("namespace")
                            )
                        else:
                            self._logger.warning(
                                "[mcp_orchestrator] No user_namespace available for tool=%s (unauthenticated request)",
                                tool_name
                            )
                    else:
                        # Gmail tools must not receive namespace; profile is sufficient for routing
                        if "namespace" in payload:
                            payload.pop("namespace", None)
                            self._logger.info(
                                "[mcp_orchestrator] Removed namespace from Gmail tool=%s payload to satisfy MCP schema",
                                tool_name
                            )

                    result = self._gateway.invoke(tool_name, payload)
                    tool_payload = self._format_tool_result(tool_name, result.payload, result.duration_ms, "ok")
                    invocations.append({"tool": tool_name, "payload": dict(payload)})
                    self._logger.info(
                        "[mcp_orchestrator] Tool invocation #%d: tool=%s, model=%s",
                        iteration_count,
                        tool_name,
                        model or "default"
                    )
                except Exception as exc:  # pragma: no cover - error handling path validated separately
                    tool_payload = self._format_tool_result(tool_name, None, None, "error", str(exc))
                    invocations.append({"tool": tool_name, "payload": dict(payload), "error": str(exc)})
                    self._logger.warning("[mcp_orchestrator] Tool %s failed: %s", tool_name, exc)

                augmented_context.append({"role": "tool", "content": tool_payload})
                tool_messages.append({"role": "tool", "content": tool_payload})

            follow_up_prompt = (
                "Provide a final answer to the user now that the tool result is available. "
                "If the tool failed, explain the error. "
                "If you need to call another tool, you may do so."
            )
            current_response = llm_client.generate(follow_up_prompt, context=augmented_context, model=model)

        # Log if we hit the iteration limit
        if iteration_count >= self._max_tool_invocations and self._is_json_action_response(current_response):
            self._logger.warning(
                "[mcp_orchestrator] Reached max tool invocation limit (%d), "
                "but LLM still wants to call tools. Returning current response.",
                self._max_tool_invocations
            )

        return OrchestratorResult(
            response_text=current_response,
            extra_messages=tuple(tool_messages),
            tool_invocations=tuple(invocations),
        )


__all__ = [
    "InternalMCPChatOrchestrator",
    "OrchestratorResult",
    "ToolCallParsingError",
]
