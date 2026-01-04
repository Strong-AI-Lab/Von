"""Chat assistant orchestration utilities for the internal MCP gateway."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import (
    Any,
    cast,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    NotRequired,
    Optional,
    Required,
    Sequence,
    TypedDict,
)

from .gateway import InternalMCPGateway

# Structured tool calling support (JVNAUTOSCI-799 Phase 3)
from ...languagemodels.structured_tool_calling import (
    LLMResponse,
    ToolDefinition,
    ToolCall,
)
from ...services.prompt_template_service import PromptTemplateService
from ...workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from ...workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    MISSING_TOOL_CALL_WORKFLOW_ID,
    register_default_workflows,
)
from ...workflows.engine import WorkflowExecutor
from ...workflows.workflow_registry import WorkflowRegistry
from ...workflows.workflow_selector import WorkflowSelector


@dataclass(frozen=True)
class OrchestratorResult:
    """Structured response from a tool-aware chat exchange."""

    response_text: str
    extra_messages: Sequence[Mapping[str, Any]]
    tool_invocations: Sequence[Mapping[str, Any]]
    aux_llm_calls: Sequence[Mapping[str, Any]]

    # Optional LLM interaction telemetry (JVNAUTOSCI-877). Append-only for backwards compatibility.
    llm_calls: Sequence[Mapping[str, Any]] = ()
    llm_usage: Mapping[str, Any] | None = None
    orchestrator_duration_ms: float | None = None


class _ToolCallRequest(TypedDict):
    action: Required[str]
    tool: Required[str]
    payload: Required[MutableMapping[str, Any]]
    _call_id: NotRequired[str]


@dataclass(frozen=True)
class _ModelTurnInterpretation:
    """Interpreted model output for a single assistant turn.

    This object centralises parsing and classification of model output so that
    higher-level policy (retry/fallback decisions) is not coupled to ad-hoc
    parsing and exception handling.
    """

    response_text: str
    tool_calls: list[_ToolCallRequest] | None
    tool_call_parse_error: ToolCallParsingError | None
    is_json_action: bool
    fenced_tool_call_json: bool
    heuristic_missing_tool_call: bool


@dataclass(frozen=True)
class _MissingToolCallAssessment:
    """Assessment result for whether a single tool-call retry should occur.

    This isolates *policy* (when to retry) from orchestration mechanics so the
    retry behaviour can evolve without tangling more logic into `run()`.
    """

    path: str
    is_json_action: bool
    fenced_json: bool
    classifier_invoked: bool
    classifier_has_verdict: bool
    # Backwards-compatible alias retained for existing debug consumers.
    # Historically this effectively meant "classifier produced a yes/no verdict".
    # It now means "classifier was invoked".
    classifier_used: bool
    classifier_verdict: bool | None
    retry_reason: str | None
    tool_call_parse_error: ToolCallParsingError | None


@dataclass(frozen=True)
class _MissingToolCallDetectorSpec:
    """Declarative definition of the missing-tool-call detector."""

    action_id: str
    prompt_id: Optional[str]
    prompt_text: str
    model: Optional[str]


class ToolCallParsingError(Exception):
    """Raised when a model returns an invalid tool call payload."""

    def __init__(self, message: str, *, raw_response: str | None = None) -> None:
        super().__init__(message)
        self.raw_response = raw_response


class InternalMCPChatOrchestrator:
    """Simple loop that allows the Von assistant to call internal tools."""

    _ACTION_FIELD = "action"
    _CALL_ACTION = "call_tool"
    _TOOL_FIELD = "tool"
    _PAYLOAD_FIELD = "payload"
    _MISSING_TOOL_CALL_ACTION_ID = "#V#detect_missing_tool_call_action"
    _FALLBACK_MISSING_TOOL_CALL_PROMPT = (
        "You are a strict classifier for an agent system that can call tools via JSON.\n"
        "Your job: decide whether the assistant response *promises* to call tools (or says it is about to do so) "
        "but does not actually emit a tool-call JSON object/array.\n\n"
        "Rules:\n"
        "- Answer ONLY 'YES' or 'NO'.\n"
        "- Answer YES if the response contains phrases like 'I will', 'I’m going to', 'Proceeding now', 'Invoking', "
        "or similar action narration *and* references tool-like actions (search, fetch, create/update, MCP, ontology).\n"
        "- Answer NO for normal explanations, summaries, or questions that are not claiming to execute tools now.\n\n"
        "Assistant response:\n"
        "{response}\n"
    )
    _MISSING_TOOL_CLASSIFIER_PROMPTS = ("#V#missing_tool_call_classifier_prompt",)
    _MISSING_TOOL_RETRY_PROMPTS = ("#V#missing_tool_call_retry_prompt",)
    _TURN_SELECTOR_PROMPTS = ("#V#chat_turn_classifier_prompt",)

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

        # Vontology-backed missing-tool-call detector (lazy loaded)
        self._missing_tool_call_detector: Optional[_MissingToolCallDetectorSpec] = None
        self._missing_tool_call_detector_loaded: bool = False

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

        self._prompt_templates = PromptTemplateService()
        self._workflow_registry = WorkflowRegistry()
        register_default_workflows(self._workflow_registry)
        self._action_registry = self._build_action_registry()
        self._workflow_executor = WorkflowExecutor(registry=self._action_registry)
        self._workflow_selector = WorkflowSelector(
            registry=self._workflow_registry,
            prompt_service=self._prompt_templates,
            verdict_mapping={
                "plain_response": CHAT_ASSISTANT_WORKFLOW_ID,
                "tool_seeking": CHAT_ASSISTANT_WORKFLOW_ID,
                "summarisation": CHAT_ASSISTANT_WORKFLOW_ID,
                "narration": CHAT_NARRATION_WORKFLOW_ID,
            },
            classifier_prompt_ids=self._TURN_SELECTOR_PROMPTS,
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

    def _build_action_registry(self) -> ActionRegistry:
        registry = ActionRegistry()
        registry.register(
            ActionSpec(
                action_id="missing_tool_call.assess",
                handler=self._action_missing_tool_call_assess,
                description="Classify and log missing tool-call verdicts.",
                concept_id=self._MISSING_TOOL_CALL_ACTION_ID,
            )
        )
        registry.register(
            ActionSpec(
                action_id="missing_tool_call.retry",
                handler=self._action_missing_tool_call_retry,
                description="Run retry prompt to elicit tool-call JSON.",
                concept_id=self._MISSING_TOOL_CALL_ACTION_ID,
            )
        )
        registry.register(
            ActionSpec(
                action_id="narration.classify",
                handler=self._action_narration_classify,
                description="Determine whether narration should run.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="narration.select_prompts",
                handler=self._action_narration_select_prompts,
                description="Resolve narration prompt fragments from Vontology.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="narration.render",
                handler=self._action_narration_render,
                description="Generate narration text via LLM.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="narration.emit_audio",
                handler=self._action_narration_emit_audio,
                description="Attach narration output to presenter channels.",
            )
        )
        # Stubs for workflow catalogue completeness (Issue 7)
        for action_id in (
            "todo_refresh.check_cache",
            "todo_refresh.fetch_gmail",
            "todo_refresh.extract_tasks",
            "todo_refresh.prioritise",
            "todo_refresh.summarise",
        ):
            registry.register(
                ActionSpec(
                    action_id=action_id,
                    handler=self._noop_action,
                    description="Placeholder action; real implementation to be bound via MCP.",
                )
            )
        return registry

    @staticmethod
    def _noop_action(request: Any) -> WorkflowActionResult:
        return WorkflowActionResult(outputs={})

    def _action_missing_tool_call_assess(self, request: Any) -> WorkflowActionResult:
        data = request.data
        aux_log = data.setdefault("aux_llm_calls", [])
        assessor = data.get("missing_tool_assessor")
        if not callable(assessor):
            return WorkflowActionResult(
                status="failed", error="missing_assessor_callable"
            )

        assessment = cast(
            _MissingToolCallAssessment,
            assessor(
                response_text=data.get("response_text", ""),
                use_structured=bool(data.get("use_structured")),
                interpretation=data.get("interpretation"),
                llm_client=request.environment.llm_client,
                model=request.environment.model,
                aux_log=aux_log,
                tool_call_parse_error=data.get("tool_call_parse_error"),
            ),
        )

        try:
            aux_log.append(
                {
                    "type": "missing_tool_call_detection",
                    "path": assessment.path,
                    "is_json_action": assessment.is_json_action,
                    "fenced_json": assessment.fenced_json,
                    "classifier_invoked": assessment.classifier_invoked,
                    "classifier_has_verdict": assessment.classifier_has_verdict,
                    "classifier_used": assessment.classifier_used,
                    "classifier_verdict": (
                        "yes"
                        if assessment.classifier_verdict is True
                        else (
                            "no"
                            if assessment.classifier_verdict is False
                            else "unavailable"
                        )
                    ),
                    "retry_reason": assessment.retry_reason or "",
                    "parse_error": (
                        str(assessment.tool_call_parse_error)
                        if assessment.tool_call_parse_error is not None
                        else ""
                    ),
                }
            )
        except Exception:
            pass

        outputs = {
            "missing_tool_call_assessment": asdict(assessment),
            "missing_tool_call_retry_needed": bool(assessment.retry_reason),
            "missing_tool_call_retry_reason": assessment.retry_reason,
            "tool_call_parse_error": data.get("tool_call_parse_error"),
        }

        if request.trace is not None:
            request.trace.metadata.setdefault("missing_tool_call", {})
            request.trace.metadata["missing_tool_call"].update(
                {
                    "retry_reason": assessment.retry_reason,
                    "classifier_verdict": assessment.classifier_verdict,
                }
            )

        return WorkflowActionResult(outputs=outputs)

    def _action_missing_tool_call_retry(self, request: Any) -> WorkflowActionResult:
        data = request.data
        aux_log = data.setdefault("aux_llm_calls", [])
        augmented_context = data.get("augmented_context") or []
        record_llm_call = data.get("record_llm_call")

        calling_path = "legacy"
        assessment = data.get("missing_tool_call_assessment")
        if isinstance(assessment, dict) and isinstance(assessment.get("path"), str):
            calling_path = str(assessment.get("path") or "legacy")

        rendered_prompt = self._prompt_templates.render_prompt(
            self._MISSING_TOOL_RETRY_PROMPTS,
            variables={},
            fallback=self._missing_tool_call_retry_prompt(),
            max_chars=4000,
        )
        prompt_text = (
            rendered_prompt.text
            if rendered_prompt is not None
            else self._missing_tool_call_retry_prompt()
        )
        if request.trace is not None:
            request.trace.record_prompt(
                prompt_id=rendered_prompt.prompt_id if rendered_prompt else None,
                resolved_prompt=prompt_text,
                variables={},
            )

        try:
            aux_log.append(
                {
                    "type": "missing_tool_call_retry",
                    "path": calling_path,
                    "mechanism": "workflow",
                    "stage": "prompt",
                    "retry_reason": data.get("missing_tool_call_retry_reason") or "",
                    "prompt_preview": prompt_text[:800],
                }
            )
        except Exception:
            pass

        llm_start = time.perf_counter()
        retry_response = request.environment.llm_client.generate(
            prompt_text, context=augmented_context, model=request.environment.model
        )
        duration_ms = (time.perf_counter() - llm_start) * 1000.0

        if callable(record_llm_call):
            record_llm_call(
                call_type="llm.generate",
                model_name=request.environment.model,
                duration_ms=duration_ms,
                usage=None,
                note="Missing tool call retry prompt (workflow).",
            )

        try:
            aux_log.append(
                {
                    "type": "missing_tool_call_retry",
                    "path": calling_path,
                    "mechanism": "workflow",
                    "stage": "response",
                    "retry_reason": data.get("missing_tool_call_retry_reason") or "",
                    "response_preview": (
                        retry_response[:800]
                        if isinstance(retry_response, str)
                        else str(retry_response)[:800]
                    ),
                }
            )
        except Exception:
            pass

        extract_fn = data.get("extract_tool_calls_fn") or self._extract_tool_calls
        retry_calls = None
        parse_error = None
        try:
            retry_calls = extract_fn(retry_response)
        except ToolCallParsingError as exc:
            parse_error = exc

        success = bool(retry_calls)
        outputs = {
            "response_text": retry_response,
            "tool_calls": retry_calls,
            "missing_tool_call_retry_success": success,
            "tool_call_parse_error": parse_error or data.get("tool_call_parse_error"),
        }

        return WorkflowActionResult(
            outputs=outputs,
            duration_ms=duration_ms,
        )

    def _action_narration_classify(self, request: Any) -> WorkflowActionResult:
        required = bool(
            request.data.get("presenter_mode_requested")
            or request.data.get("force_narration")
        )
        return WorkflowActionResult(
            outputs={
                "narration_required": required,
            }
        )

    def _action_narration_select_prompts(self, request: Any) -> WorkflowActionResult:
        prompt_ids = request.data.get("narration_prompt_ids") or ()
        fallback_text = request.data.get("narration_prompt_text")
        rendered = self._prompt_templates.render_prompt(
            prompt_ids,
            fallback=fallback_text,
            variables={},
            max_chars=6000,
        )
        prompt_text = rendered.text if rendered else fallback_text
        outputs = {
            "narration_prompt_text": prompt_text,
            "narration_prompt_id": rendered.prompt_id if rendered else None,
            "narration_prompts_resolved": bool(prompt_text),
        }
        if request.trace is not None and prompt_text:
            request.trace.record_prompt(
                prompt_id=rendered.prompt_id if rendered else None,
                resolved_prompt=prompt_text,
                variables={},
            )
        return WorkflowActionResult(outputs=outputs)

    @staticmethod
    def _coerce_spoken_text(text: object) -> str | None:
        if text is None:
            return None
        raw = str(text).strip()
        if not raw:
            return None

        import re

        match = re.search(
            r"<spoken>\s*(.*?)\s*</spoken>", raw, flags=re.DOTALL | re.IGNORECASE
        )
        if match:
            raw = match.group(1)

        raw = re.sub(r"</?spoken>", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"</?screen>", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
        raw = raw.replace("`", "").strip()
        if not raw:
            return None
        max_chars = 800
        if len(raw) > max_chars:
            raw = raw[:max_chars].rstrip()
        return raw or None

    def _action_narration_render(self, request: Any) -> WorkflowActionResult:
        prompt_text = request.data.get("narration_prompt_text") or ""
        screen_text = request.data.get("screen_text") or ""
        user_prompt = request.data.get("user_prompt") or ""

        narration_system = (
            "You are Von. Produce a short talk track for text-to-speech. "
            "Return ONLY one block: <spoken>...</spoken>. "
            "Do not include <screen>. Do not include code blocks. "
            "Use New Zealand English spelling."
        )
        if prompt_text:
            narration_system += "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
            narration_system += prompt_text

        narration_user = (
            "User message:\n"
            f"{user_prompt}\n\n"
            "On-screen content (do not read verbatim if long; summarise):\n"
            f"{screen_text}\n"
        )

        narration_response = request.environment.llm_client.generate(
            prompt="Generate <spoken> talk track",
            context=[
                {"role": "system", "content": narration_system},
                {"role": "user", "content": narration_user},
            ],
            model=request.environment.model,
        )

        spoken = self._coerce_spoken_text(narration_response)
        if not spoken:
            spoken = self._coerce_spoken_text(screen_text)

        outputs = {
            "narration_rendered": bool(spoken),
            "narration_spoken": spoken,
            "narration_raw_response": narration_response,
        }
        return WorkflowActionResult(outputs=outputs)

    def _action_narration_emit_audio(self, request: Any) -> WorkflowActionResult:
        spoken = request.data.get("narration_spoken")
        presenter_channels = request.data.get("presenter_channels") or {}
        if not isinstance(presenter_channels, dict):
            presenter_channels = {}
        screen_text = request.data.get("screen_text")
        if screen_text and "screen" not in presenter_channels:
            presenter_channels["screen"] = screen_text
        if spoken:
            presenter_channels["spoken"] = spoken
        return WorkflowActionResult(
            outputs={
                "presenter_channels": presenter_channels,
                "narration_emitted": bool(spoken),
            }
        )

    def _convert_mcp_tools_to_structured_definitions(
        self,
    ) -> List[ToolDefinition]:
        """Convert MCP tool catalog to structured ToolDefinition list (JVNAUTOSCI-799).

        Returns:
            List of ToolDefinition objects describing available MCP tools
        """
        catalogue = self._gateway.describe_methods()
        tool_definitions: List[ToolDefinition] = []

        for tool_name, metadata in catalogue.items():
            try:
                # Convert MCP Schema to JSON Schema format
                input_schema = self._mcp_schema_to_json_schema(
                    metadata.get("input_schema", {})
                )

                # Create ToolDefinition
                tool_def = ToolDefinition(
                    name=tool_name,
                    description=metadata.get("description", f"Execute {tool_name}"),
                    input_schema=input_schema,
                )
                tool_definitions.append(tool_def)
            except Exception as exc:
                self._logger.warning(
                    "[orchestrator] Failed to convert tool %s to ToolDefinition: %s",
                    tool_name,
                    exc,
                )
                continue

        self._logger.debug(
            "[orchestrator] Converted %d MCP tools to ToolDefinitions",
            len(tool_definitions),
        )
        return tool_definitions

    def _mcp_schema_to_json_schema(
        self, mcp_schema: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Convert MCP Schema format to JSON Schema format.

        MCP schemas use required/optional dicts, JSON Schema uses properties + required list.
        """
        required_fields = mcp_schema.get("required", {})
        optional_fields = mcp_schema.get("optional", {})

        properties: Dict[str, Any] = {}
        required_list: List[str] = []

        # Process required fields
        for field_name, field_type in required_fields.items():
            properties[field_name] = {
                "type": self._python_type_to_json_schema_type(field_type)
            }
            required_list.append(field_name)

        # Process optional fields
        for field_name, field_type in optional_fields.items():
            properties[field_name] = {
                "type": self._python_type_to_json_schema_type(field_type)
            }

        json_schema = {
            "type": "object",
            "properties": properties,
        }

        if required_list:
            json_schema["required"] = required_list

        return json_schema

    @staticmethod
    def _python_type_to_json_schema_type(python_type: Any) -> str:
        """Convert Python type annotations to JSON Schema type strings."""
        # Handle tuples of types (union types)
        if isinstance(python_type, tuple):
            # For unions, just take the first non-None type
            for t in python_type:
                if t is not type(None):
                    return InternalMCPChatOrchestrator._python_type_to_json_schema_type(
                        t
                    )
            return "string"  # Fallback

        # Handle None type
        if python_type is type(None):
            return "null"

        # Map Python types to JSON Schema types
        type_map = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }

        return type_map.get(python_type, "string")  # Default to string

    def _limit_context_for_llm(
        self, messages: List[Mapping[str, Any]]
    ) -> List[Mapping[str, Any]]:
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

    def _truncate_nested_for_llm(
        self, value: Any, *, max_string_chars: int, max_list_items: int = 50
    ) -> Any:
        """Truncate nested tool payloads so they remain useful but bounded."""
        if isinstance(value, str):
            if len(value) <= max_string_chars:
                return value
            return (
                value[:max_string_chars]
                + f"\n... [truncated {len(value) - max_string_chars} chars]"
            )
        if isinstance(value, list):
            if len(value) <= max_list_items:
                return [
                    self._truncate_nested_for_llm(
                        v,
                        max_string_chars=max_string_chars,
                        max_list_items=max_list_items,
                    )
                    for v in value
                ]
            head = value[: max_list_items - 1]
            tail = value[-1:]
            truncated = [
                *[
                    self._truncate_nested_for_llm(
                        v,
                        max_string_chars=max_string_chars,
                        max_list_items=max_list_items,
                    )
                    for v in head
                ],
                {
                    "_truncated": True,
                    "_omitted_items": len(value) - len(head) - len(tail),
                },
                *[
                    self._truncate_nested_for_llm(
                        v,
                        max_string_chars=max_string_chars,
                        max_list_items=max_list_items,
                    )
                    for v in tail
                ],
            ]
            return truncated
        if isinstance(value, dict):
            return {
                str(k): self._truncate_nested_for_llm(
                    v, max_string_chars=max_string_chars, max_list_items=max_list_items
                )
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
            auth_status = f"\n\n🔐 AUTHENTICATION STATUS: Authenticated (namespace: {user_namespace})\nRAG tools (rag_list_collections, search_knowledge_base, rag_list_indexed, rag_get_item) are AVAILABLE.\n"
        else:
            auth_status = (
                "\n\n⚠️ AUTHENTICATION STATUS: NOT AUTHENTICATED\n"
                "RAG tools (rag_list_collections, search_knowledge_base, rag_list_indexed, rag_get_item) are UNAVAILABLE.\n"
                "These tools require user authentication to prevent cross-user data access.\n"
                "If user asks about their RAG data/sessions/indexed content, explain they need to log in first.\n"
            )

        base_message = (
            "You have access to internal MCP tools.\n\n"
            "⚠️ WHEN TO USE TOOLS (CHECK THESE FIRST) ⚠️\n"
            "If user asks for RECENT, CURRENT, LATEST, NEW, or BREAKING information → USE search_web\n"
            'If user mentions specific dates (2024+, 2025+, "this year", "this month") → USE search_web\n'
            'If user explicitly says "search", "look up", "find information on" → USE search_web\n'
            'If user asks "what\'s new", "recent developments", "latest research" → USE search_web\n'
            "If user provides a URL to analyse or extract content from → USE extract_url (or resilient_extract_url for JS-heavy/blocked pages)\n"
            "If user asks a direct factual question needing verification → USE qna_search\n"
            "If searching within specific domain/context (e.g., site:example.com) → USE context_search\n"
            "If user asks about arXiv papers by author, topic, or ID → USE list_papers or read_paper\n"
            'If user asks "what\'s in my RAG store?" or asks about RAG *collections/sources* → USE rag_list_collections\n'
            'If user asks about RAG sessions ("how many", "what\'s indexed", "list sessions") → USE rag_list_indexed (often with collection=...)\n'
            "If user wants to see RAG content from a specific session → USE rag_get_item (often with collection=...)\n"
            "If user wants semantic/topic search over indexed content → USE search_knowledge_base\n\n"
            "CRITICAL: Your training data has a cutoff date. For anything described as current/recent/new, "
            "you MUST use search tools to get up-to-date information.\n\n"
            "HOW TO INVOKE A TOOL:\n"
            "Respond with EITHER a single tool-call JSON object OR a JSON array of tool-call objects:\n"
            "Single:\n"
            '{"action": "call_tool", "tool": "tool_name", "payload": {"param": "value"}}\n'
            "Batch (preferred for multi-step workflows; keep it small):\n"
            '[{"action": "call_tool", "tool": "tool_a", "payload": {}}, {"action": "call_tool", "tool": "tool_b", "payload": {}}]\n\n'
            "INVOCATION RULES:\n"
            "- DO NOT explain what you're going to do - just do it\n"
            "- DO NOT output JSON as an example or description - only output JSON when you want to invoke a tool NOW\n"
            "- DO NOT say 'I will call' or 'Let me call' - just call it\n"
            "- You MAY batch multiple tool calls in ONE message as a JSON array (keep it to <= 4 calls)\n"
            "- After you receive the tool result (role 'tool'), respond naturally to the user\n\n"
            "VERIFICATION & CONSISTENCY RULES:\n"
            "- If the user doubts whether a specific concept_id exists (e.g. '#V#...') or challenges a claim about Vontology state, ALWAYS verify first using fetch_concept (or search_concepts) before responding.\n"
            "- Do NOT reply with prose-only 'we should verify' / 'I will not assert unless I can verify' without actually calling a tool.\n\n"
            "If you output JSON, the system will execute that tool call immediately.\n\n"
            "IMPORTANT: arXiv paper conversions (PDF to markdown) can take 5-10 minutes.\n"
            "If read_paper fails, the paper may still be converting. Check with list_papers.\n\n"
            "Available tools:\n"
            f"{listing}"
        )

        if (
            auxiliary_system_prompt
            and isinstance(auxiliary_system_prompt, str)
            and auxiliary_system_prompt.strip()
        ):
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

        # Normalise common Unicode punctuation (smart quotes) so heuristics behave
        # consistently across models and frontends.
        normalised = (
            text.replace("\u2019", "'")
            .replace("\u2018", "'")
            .replace("\u2032", "'")
            .replace("\u201c", '"')
            .replace("\u201d", '"')
        )
        lowered = normalised.lower()
        triggers = (
            "here is the actual ontology operation",
            "here's the actual ontology operation",
            "here is the actual tool call",
            "here's the actual tool call",
            "here is the actual ontology operation.",
            "here is the actual ontology operation:",
            "here is the actual operation",
            "here's the actual operation",
            # Common narration patterns that precede no-op responses
            "proceeding now",
            "what i am about to apply",
            "what i'm about to apply",
            "i will now",
            "i'll now",
        )
        if any(t in lowered for t in triggers):
            return True

        # Detect "promise to do" pattern when combined with tool-related keywords
        # This catches "I'm going to search..." or "I'll execute the tools now"
        toolish_keywords = (
            "search",
            "web",
            "fetch",
            "execute",
            "tool",
            "ontology",
            "mcp",
            "operation",
            "concept",
            "relationship",
            "create",
            "link",
            "add",
            "update",
        )
        promise_patterns = (
            "i'm going to",
            "i am going to",
            "i'll",
            "i will",
            "let me",
            "i'll fix that now",
            "i'll do that now",
            "i'll start",
        )

        has_promise = any(p in lowered for p in promise_patterns)
        has_toolish = any(k in lowered for k in toolish_keywords)

        if has_promise and has_toolish:
            # Additional check: the response should be relatively short (< 500 chars)
            # to avoid triggering on long explanatory responses
            if len(text) < 500:
                return True

        # Secondary trigger: explicit step-by-step change description without any tool JSON.
        # Only count as missing-tool-call if the assistant also uses tool-y language.
        toolish = ("ontology", "mcp", "tool", "operation")
        if any(k in lowered for k in toolish):
            if "->" in text or "→" in text:
                return True

        return False

    def _first_relationship_value(
        self, concept: Mapping[str, Any] | None, predicate: str
    ) -> Optional[str]:
        """Return the first string relationship target for a predicate."""

        if not concept:
            return None

        relationships = (
            concept.get("relationships") if isinstance(concept, Mapping) else None
        )
        if not isinstance(relationships, Mapping):
            return None

        raw = relationships.get(predicate)
        if isinstance(raw, str):
            return raw.strip() or None
        if isinstance(raw, Iterable):
            for candidate in raw:
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return None

    def _get_text_content_for_concept(self, concept_id: str) -> Optional[str]:
        """Resolve content for a concept using preserved fields or text relations."""

        if not isinstance(concept_id, str) or not concept_id.strip():
            return None

        concept_id = concept_id.strip()

        try:
            from src.backend.services.concept_service import get_concept_by_concept_id
        except Exception:
            return None

        try:
            concept = get_concept_by_concept_id(concept_id)
        except Exception:
            concept = None

        candidate_texts: List[Dict[str, Any]] = []

        def _add_candidate(
            text_value: str, *, predicate: str = "hasContent", lang: str = ""
        ) -> None:
            if isinstance(text_value, str) and text_value.strip():
                candidate_texts.append(
                    {"text": text_value.strip(), "predicate": predicate, "lang": lang}
                )

        if isinstance(concept, Mapping):
            direct_content = concept.get("content")
            if isinstance(direct_content, str):
                _add_candidate(direct_content, predicate="direct", lang="")

            preserved = (
                concept.get("concept_data", {})
                if isinstance(concept.get("concept_data"), Mapping)
                else {}
            )
            preserved_fields = (
                preserved.get("preserved_fields", {})
                if isinstance(preserved.get("preserved_fields"), Mapping)
                else {}
            )
            preserved_content = preserved_fields.get("content")
            if isinstance(preserved_content, str):
                _add_candidate(
                    preserved_content, predicate="preserved_content", lang=""
                )

        try:
            from src.backend.services.text_value_service import get_texts_for_concept

            texts = get_texts_for_concept(concept_id)
        except Exception:
            texts = None

        if isinstance(texts, list):
            for text in texts:
                if not isinstance(text, dict):
                    continue
                predicate = text.get("predicate")
                if predicate not in {"hasContent", "hasDescription"}:
                    continue
                text_value = text.get("text")
                if not isinstance(text_value, str) or not text_value.strip():
                    continue
                raw_lang = text.get("lang")
                lang = raw_lang if isinstance(raw_lang, str) else ""
                _add_candidate(text_value, predicate=predicate, lang=lang)

        if not candidate_texts:
            return None

        def _sort_key(item: Dict[str, Any]) -> tuple[int, int, str]:
            predicate = item.get("predicate")
            lang = item.get("lang")

            predicate_rank = 0 if predicate == "hasContent" else 1
            lang_rank = 0 if lang in {"en-NZ", "en"} else 1
            # Stable ordering
            return (predicate_rank, lang_rank, item.get("predicate", ""))

        candidate_texts.sort(key=_sort_key)
        best = candidate_texts[0].get("text")
        return best if isinstance(best, str) else None

    @staticmethod
    def _normalise_llm_model_name(model: Optional[str]) -> Optional[str]:
        """Convert internal model identifiers into provider model names.

        The Vontology often stores model references as concept IDs (e.g. "#V#gpt-4o-mini").
        LLM clients expect provider model strings (e.g. "gpt-4o-mini").

        This is intentionally lightweight so other LLM workflows/actions can reuse it.
        """

        if not isinstance(model, str):
            return None

        candidate = model.strip()
        if not candidate:
            return None

        if candidate.startswith("#V#"):
            candidate = candidate[3:]

        return candidate or None

    def _inject_prompt_variable(self, prompt_text: str, *, key: str, value: str) -> str:
        """Ensure a prompt receives a variable payload.

        Preferred: replace "{key}" if present.
        Fallback: append a small labelled section.

        This keeps future LLM actions easy to add without requiring every stored
        prompt to strictly follow a single templating convention.
        """

        if not isinstance(prompt_text, str):
            prompt_text = ""

        placeholder = "{" + key + "}"
        if placeholder in prompt_text:
            return prompt_text.replace(placeholder, value)

        # One-time warning per instance to avoid noisy logs.
        warn_attr = f"_warned_missing_placeholder_{key}"
        if not getattr(self, warn_attr, False):
            try:
                self._logger.info(
                    "[mcp_orchestrator] Prompt missing placeholder %s; appending variable payload.",
                    placeholder,
                )
            except Exception:  # pragma: no cover
                pass
            setattr(self, warn_attr, True)

        return f"{prompt_text.rstrip()}\n\n{key.replace('_', ' ').title()}:\n{value}\n"

    def _get_missing_tool_call_detector(self) -> Optional[_MissingToolCallDetectorSpec]:
        """Load the missing-tool-call detector spec from the Vontology (best effort)."""

        fallback_enabled = os.getenv(
            "VON_MISSING_TOOL_CALL_CLASSIFIER_FALLBACK", "1"
        ).lower() in {"1", "true"}

        def _fallback_spec() -> Optional[_MissingToolCallDetectorSpec]:
            if not fallback_enabled:
                return None
            return _MissingToolCallDetectorSpec(
                action_id="fallback_missing_tool_call_detector",
                prompt_id=None,
                prompt_text=self._FALLBACK_MISSING_TOOL_CALL_PROMPT,
                model=None,
            )

        if self._missing_tool_call_detector_loaded:
            return self._missing_tool_call_detector or _fallback_spec()

        self._missing_tool_call_detector_loaded = True

        try:
            from src.backend.services.concept_service import get_concept_by_concept_id
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.info(
                "[mcp_orchestrator] Missing tool-call detector unavailable (concept_service import failed): %s",
                exc,
            )
            self._missing_tool_call_detector = None
            return None

        try:
            action = get_concept_by_concept_id(self._MISSING_TOOL_CALL_ACTION_ID)
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.info(
                "[mcp_orchestrator] Missing tool-call detector action not found (%s): %s",
                self._MISSING_TOOL_CALL_ACTION_ID,
                exc,
            )
            action = None

        if not isinstance(action, Mapping):
            self._missing_tool_call_detector = _fallback_spec()
            return self._missing_tool_call_detector

        prompt_id = self._first_relationship_value(action, "#V#uses_prompt")
        model_id = self._first_relationship_value(action, "#V#uses_llm_model")

        prompt_text: Optional[str] = None
        if isinstance(prompt_id, str):
            prompt_text = self._get_text_content_for_concept(prompt_id)

        if not prompt_text:
            self._logger.info(
                "[mcp_orchestrator] Missing tool-call detector prompt unavailable for action=%s (prompt_id=%s)",
                self._MISSING_TOOL_CALL_ACTION_ID,
                prompt_id or "",
            )
            self._missing_tool_call_detector = _fallback_spec()
            return self._missing_tool_call_detector

        self._missing_tool_call_detector = _MissingToolCallDetectorSpec(
            action_id=self._MISSING_TOOL_CALL_ACTION_ID,
            prompt_id=prompt_id,
            prompt_text=prompt_text,
            model=model_id or None,
        )
        return self._missing_tool_call_detector

    def _llm_detects_missing_tool_call(
        self,
        response: str,
        llm_client: Any,
        *,
        fallback_model: Optional[str],
        aux_log: Optional[List[Mapping[str, Any]]] = None,
        path: str | None = None,
    ) -> tuple[bool, Optional[bool]]:
        """Run the Vontology-configured detector LLM to classify the response.

        Returns (invoked, verdict).

        invoked:
            True if we attempted to call the classifier LLM.
        verdict:
            True if the classifier says the model promised a tool call but didn't
            emit one, False if the classifier says no, and None if no yes/no
            verdict could be obtained (unavailable prompt/model, errors, or
            unexpected output).
        """

        if not isinstance(response, str) or not response.strip():
            return False, None

        detector = self._get_missing_tool_call_detector()
        if not detector or not detector.prompt_text:
            return False, None

        # Avoid ballooning the classifier input; we only need the last response.
        truncated_response = response.strip()
        max_chars = 4000
        if len(truncated_response) > max_chars:
            truncated_response = (
                truncated_response[:max_chars]
                + f"\n... [truncated {len(truncated_response) - max_chars} chars]"
            )

        raw_model_name = detector.model or fallback_model
        model_name = self._normalise_llm_model_name(raw_model_name)

        placeholder_present = (
            isinstance(detector.prompt_text, str)
            and "{response}" in detector.prompt_text
        )
        injection_mode = "replace" if placeholder_present else "append"
        prompt_text = self._inject_prompt_variable(
            detector.prompt_text,
            key="response",
            value=truncated_response,
        )

        try:
            classifier_output = llm_client.generate(
                prompt_text, context=None, model=model_name
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.warning(
                "[mcp_orchestrator] Missing tool-call classifier failed (model=%s): %s",
                model_name or "default",
                exc,
            )
            return True, None

        if not isinstance(classifier_output, str):
            return True, None

        try:
            if aux_log is not None:
                aux_log.append(
                    {
                        "type": "missing_tool_call_classifier",
                        "path": path or "",
                        "model": model_name or fallback_model or "default",
                        "model_raw": raw_model_name or "",
                        "model_resolved": model_name or "",
                        "prompt_placeholder_response": placeholder_present,
                        "prompt_injection_mode": injection_mode,
                        "prompt_preview": prompt_text[:800],
                        "response_preview": classifier_output[:800],
                        "truncated": len(prompt_text) > 800
                        or len(classifier_output) > 800,
                    }
                )
        except Exception:  # pragma: no cover - defensive
            pass

        verdict_text = classifier_output.strip().lower()
        if verdict_text.startswith("yes"):
            return True, True
        if verdict_text.startswith("no"):
            return True, False

        return True, None

    @staticmethod
    def _contains_fenced_tool_call_json(response: str) -> bool:
        """Detect if response contains a fenced JSON block with tool-call structure.

        This catches the pattern where the model outputs prose followed by a
        fenced code block containing valid tool-call JSON, but the strict
        extraction logic rejected it (due to too much leading prose).

        Only triggers if the fenced content parses to a dict/list with tool-call shape.
        """

        if not isinstance(response, str):
            return False

        # Look for ```json or ``` fences
        fence_patterns = [
            ("```json\n", "\n```"),
            ("```\n", "\n```"),
        ]

        for open_fence, close_fence in fence_patterns:
            start = response.find(open_fence)
            if start == -1:
                continue
            content_start = start + len(open_fence)
            end = response.find(close_fence, content_start)
            if end == -1:
                continue

            fenced_content = response[content_start:end].strip()
            if not fenced_content:
                continue

            # Try to parse as JSON
            try:
                parsed = json.loads(fenced_content)
            except (json.JSONDecodeError, ValueError):
                continue

            # Check if it looks like a tool call (single object or array)
            if isinstance(parsed, dict):
                if "tool" in parsed and "payload" in parsed:
                    return True
                if "action" in parsed and parsed.get("action") == "call_tool":
                    return True
            elif isinstance(parsed, list):
                if not parsed:
                    continue
                # Check if it's an array of tool calls
                first = parsed[0]
                if isinstance(first, dict):
                    if "tool" in first and "payload" in first:
                        return True
                    if "action" in first and first.get("action") == "call_tool":
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
            (stripped.startswith("{") and stripped.endswith("}"))
            or (stripped.startswith("[") and stripped.endswith("]"))
        ):
            return False

        # Check if it contains action/tool/payload structure
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                if not parsed:
                    return False
                return any(
                    self._is_json_action_response(json.dumps(item)) for item in parsed
                )
            if not isinstance(parsed, dict):
                return False

            # Exact match for tool call pattern
            has_action = parsed.get(self._ACTION_FIELD) == self._CALL_ACTION
            has_tool = self._TOOL_FIELD in parsed and isinstance(
                parsed[self._TOOL_FIELD], str
            )
            has_payload = self._PAYLOAD_FIELD in parsed and isinstance(
                parsed[self._PAYLOAD_FIELD], dict
            )

            # Some models omit the action field or emit tool-call-like JSON with
            # extra diagnostics keys (e.g., status/duration_ms). Treat these as a
            # likely tool-call attempt for diagnostics/recovery.
            allowed_tool_like_keys = {
                self._ACTION_FIELD,
                self._TOOL_FIELD,
                self._PAYLOAD_FIELD,
                "status",
                "duration_ms",
                "error",
                "_call_id",
            }
            missing_action_but_tool_shape = (
                self._ACTION_FIELD not in parsed
                and has_tool
                and has_payload
                and set(parsed.keys()) <= allowed_tool_like_keys
            )

            return (
                has_action and has_tool and has_payload
            ) or missing_action_but_tool_shape
        except (json.JSONDecodeError, TypeError):
            return False

    def _extract_tool_calls(self, text: str) -> list[_ToolCallRequest] | None:
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
            prefix_ok = not prefix.strip() or (
                len(prefix) <= 200
                and prefix.count("\n") <= 2
                and all(token not in prefix for token in ("{", "[", "}"))
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

        looks_like_tool_call = any(
            token in raw
            for token in (
                f'"{self._ACTION_FIELD}"',
                f'"{self._TOOL_FIELD}"',
                f'"{self._PAYLOAD_FIELD}"',
                '"call_tool"',
            )
        )

        if not (raw.startswith("{") or raw.startswith("[")):
            return None

        # Avoid raising parse errors for non-tool JSON (e.g., when the model
        # returns a JSON answer or the user pasted JSON). Only attempt to parse
        # tool calls when the response looks tool-shaped.
        if not looks_like_tool_call:
            return None

        decoder = json.JSONDecoder()
        try:
            parsed, end = decoder.raw_decode(raw)
        except json.JSONDecodeError as exc:
            raise ToolCallParsingError(
                "Tool call was not executed: invalid JSON in tool call response.",
                raw_response=text,
            ) from exc

        trailing = raw[end:].strip()

        def _normalise_tool_call(candidate: Any) -> _ToolCallRequest | None:
            if not isinstance(candidate, MutableMapping):
                return None

            tool_name = candidate.get(self._TOOL_FIELD)
            payload_value = candidate.get(self._PAYLOAD_FIELD)
            action_value = candidate.get(self._ACTION_FIELD)

            has_tool = isinstance(tool_name, str)
            has_payload = isinstance(payload_value, MutableMapping)
            has_action = action_value == self._CALL_ACTION

            missing_action = self._ACTION_FIELD not in candidate
            strict_shape = set(candidate.keys()) <= {
                self._TOOL_FIELD,
                self._PAYLOAD_FIELD,
            }

            if missing_action and has_tool and has_payload and strict_shape:
                catalogue = None
                describe_methods = getattr(self._gateway, "describe_methods", None)
                if callable(describe_methods):
                    catalogue = describe_methods()
                if isinstance(catalogue, Mapping) and tool_name in catalogue:
                    has_action = True
                else:
                    return None

            if not (has_action and has_tool and has_payload):
                return None

            tool_call: _ToolCallRequest = {
                self._ACTION_FIELD: self._CALL_ACTION,
                self._TOOL_FIELD: tool_name,
                self._PAYLOAD_FIELD: cast(MutableMapping[str, Any], payload_value),
            }

            call_id = candidate.get("_call_id")
            if isinstance(call_id, str) and call_id:
                tool_call["_call_id"] = call_id

            return tool_call

        tool_calls: list[_ToolCallRequest] = []
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
                            "Tool call was not executed: tool call batch must be a JSON array of tool-call objects.",
                            raw_response=text,
                        )
                    return None
                tool_calls.append(tool_call)
            if not tool_calls:
                return None
        else:
            if looks_like_tool_call:
                raise ToolCallParsingError(
                    "Tool call was not executed: tool call must be a JSON object or array.",
                    raw_response=text,
                )
            return None

        # Reject concatenated JSON values.
        if trailing:
            if trailing.startswith("{") or trailing.startswith("["):
                raise ToolCallParsingError(
                    "Tool call was not executed: multiple JSON values were emitted in one response.",
                    raw_response=text,
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
            if post_fence_trailing.startswith("{") or post_fence_trailing.startswith(
                "["
            ):
                raise ToolCallParsingError(
                    "Tool call was not executed: multiple JSON values were emitted in one response.",
                    raw_response=text,
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
            prefix_ok = not prefix.strip() or (
                len(prefix) <= 200
                and prefix.count("\n") <= 2
                and all(token not in prefix for token in ("{", "[", "}"))
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

        looks_like_tool_call = any(
            token in raw
            for token in (f'"{self._ACTION_FIELD}"', f'"{self._TOOL_FIELD}"')
        )

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
                "Tool call was not executed: invalid JSON in tool call response.",
                raw_response=text,
            ) from exc

        is_tool_call = False
        if isinstance(parsed, MutableMapping):
            has_tool = isinstance(parsed.get(self._TOOL_FIELD), str)
            has_payload = isinstance(
                parsed.get(self._PAYLOAD_FIELD, {}), MutableMapping
            )

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
                    "Tool call was not executed: multiple tool calls were emitted in one response.",
                    raw_response=text,
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
            if post_fence_trailing.startswith("{") or post_fence_trailing.startswith(
                "["
            ):
                raise ToolCallParsingError(
                    "Tool call was not executed: multiple tool calls were emitted in one response.",
                    raw_response=text,
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
                    "Tool call was not executed: tool call must be a JSON object.",
                    raw_response=text,
                )
            return None

        # Only surface parsed JSON when it is a valid tool call. This avoids
        # accidentally treating arbitrary JSON responses as tool requests.
        if not is_tool_call:
            return None

        return parsed

    def _format_tool_result(
        self,
        tool_name: str,
        payload: Any,
        duration_ms: float | None,
        status: str,
        error: str | None = None,
    ) -> str:
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
                preview_str = (
                    preview_str[: self._max_tool_result_chars] + "\n... [truncated]"
                )
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

        # Preserve presenter-mode protocol messages even when context trimming
        # would otherwise drop them (system message at index 0 is always kept).
        presenter_protocol: str | None = None
        filtered: list[Mapping[str, Any]] = []
        for msg in base:
            role = msg.get("role") if isinstance(msg, Mapping) else None
            content = msg.get("content") if isinstance(msg, Mapping) else None
            if (
                role == "system"
                and isinstance(content, str)
                and content.lstrip().startswith("PRESENTER MODE PROTOCOL:")
            ):
                presenter_protocol = content.strip()
                continue
            filtered.append(msg)
        base = [dict(m) if isinstance(m, Mapping) else m for m in filtered]

        instruction_msg = self._instruction_message(
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
        )

        if presenter_protocol:
            instruction_msg = f"{instruction_msg}\n\n{presenter_protocol}\n"

        # Log the size of the instruction message for diagnostics
        instruction_chars = len(instruction_msg)
        if instruction_chars > 10000:
            self._logger.warning(
                "[mcp_orchestrator] Large instruction message: %d chars (%d KB). "
                "This may cause token limit issues.",
                instruction_chars,
                instruction_chars // 1024,
            )

        base.insert(0, {"role": "system", "content": instruction_msg})
        return self._limit_context_for_llm(base)

    def _interpret_model_turn(self, response: Any) -> _ModelTurnInterpretation:
        """Parse and classify a single model response.

        Never raises `ToolCallParsingError`. If parsing fails, the error is
        captured in the returned interpretation so recovery policy can be
        applied consistently.
        """

        text = response if isinstance(response, str) else str(response)

        is_json_action = self._is_json_action_response(text)
        fenced_detected = self._contains_fenced_tool_call_json(text)
        heuristic_missing = self._looks_like_missing_tool_call(text)

        tool_calls: list[_ToolCallRequest] | None = None
        tool_call_parse_error: ToolCallParsingError | None = None
        try:
            tool_calls = self._extract_tool_calls(text)
        except ToolCallParsingError as exc:
            tool_call_parse_error = exc

        return _ModelTurnInterpretation(
            response_text=text,
            tool_calls=tool_calls,
            tool_call_parse_error=tool_call_parse_error,
            is_json_action=is_json_action,
            fenced_tool_call_json=fenced_detected,
            heuristic_missing_tool_call=heuristic_missing,
        )

    def _assess_missing_tool_call(
        self,
        *,
        response_text: str,
        use_structured: bool,
        interpretation: _ModelTurnInterpretation | None,
        llm_client: Any,
        model: str | None,
        aux_log: list[Mapping[str, Any]],
        tool_call_parse_error: ToolCallParsingError | None,
    ) -> _MissingToolCallAssessment:
        """Decide whether to attempt a single retry for a missing tool call."""

        path = "structured" if use_structured else "legacy"

        is_json_action = (
            interpretation.is_json_action
            if (not use_structured and interpretation is not None)
            else self._is_json_action_response(response_text)
        )

        fenced_detected = (
            interpretation.fenced_tool_call_json
            if (not use_structured and interpretation is not None)
            else self._contains_fenced_tool_call_json(response_text)
        )

        heuristic_missing = (
            interpretation.heuristic_missing_tool_call
            if (not use_structured and interpretation is not None)
            else self._looks_like_missing_tool_call(response_text)
        )

        classifier_invoked = False
        classifier_has_verdict = False
        classifier_verdict: bool | None = None
        retry_reason: str | None = None

        if tool_call_parse_error is not None:
            retry_reason = "tool call parse error"

        if retry_reason is None and fenced_detected:
            retry_reason = "fenced tool-call JSON detected"

        # If the model emits a tool-call-like JSON blob (often a tool result shape)
        # but we did not extract an executable tool call, retry once and demand a
        # pure tool-call JSON object/array.
        if retry_reason is None and is_json_action:
            retry_reason = "JSON tool-call output detected"

        if retry_reason is None:
            lowered = response_text.lower() if isinstance(response_text, str) else ""
            mentions_tools = any(
                token in lowered
                for token in (
                    "tool",
                    "mcp",
                    "ontology",
                    "rag",
                    "search",
                    "fetch",
                    "create",
                    "update",
                    "delete",
                    "jira",
                    "confluence",
                    "gmail",
                    "arxiv",
                )
            )

            detector = self._get_missing_tool_call_detector()
            fallback_detector = bool(
                detector
                and isinstance(getattr(detector, "action_id", None), str)
                and detector.action_id == "fallback_missing_tool_call_detector"
            )

            # If the conservative heuristic already fires and we're using the
            # fallback classifier spec, skip the classifier call to avoid
            # unnecessary extra LLM traffic (and to keep recovery deterministic
            # in unit tests that stub the LLM client).
            if heuristic_missing and fallback_detector:
                retry_reason = "heuristic missing tool call"
                llm_flag = None
                classifier_invoked = False
            elif not heuristic_missing and not mentions_tools:
                # Normal prose: don't bother running the classifier.
                llm_flag = None
                classifier_invoked = False
            else:
                classifier_invoked, llm_flag = self._llm_detects_missing_tool_call(
                    response_text,
                    llm_client,
                    fallback_model=model,
                    aux_log=aux_log,
                    path=path,
                )

            if llm_flag is not None:
                classifier_has_verdict = True
                classifier_verdict = llm_flag

            if retry_reason is None:
                if llm_flag is True:
                    retry_reason = "LLM classifier flagged missing tool call"
                elif llm_flag is None:
                    # Fallback to legacy heuristic only when classifier unavailable.
                    if heuristic_missing:
                        retry_reason = "heuristic missing tool call"
                elif llm_flag is False:
                    # Backstop: the LLM classifier can miss obvious cases.
                    if heuristic_missing:
                        retry_reason = (
                            "heuristic missing tool call (classifier said no)"
                        )

        return _MissingToolCallAssessment(
            path=path,
            is_json_action=is_json_action,
            fenced_json=fenced_detected,
            classifier_invoked=classifier_invoked,
            classifier_has_verdict=classifier_has_verdict,
            classifier_used=classifier_invoked,
            classifier_verdict=classifier_verdict,
            retry_reason=retry_reason,
            tool_call_parse_error=tool_call_parse_error,
        )

    def _missing_tool_call_retry_prompt(self) -> str:
        return (
            "Your previous message described an action that requires MCP tools, but you did not emit a tool call. "
            "NOW respond with ONLY a tool-call JSON object or a JSON array of tool-call objects (no prose, no Markdown)."
        )

    def execute_workflow(
        self,
        workflow_id: str,
        *,
        data: dict[str, Any],
        llm_client: Any,
        model: Optional[str],
        user_namespace: Optional[str] = None,
        auxiliary_system_prompt: str | None = None,
        trace: Any | None = None,
    ):
        workflow_def = self._workflow_registry.get(workflow_id)
        if workflow_def is None:
            return None
        env = WorkflowEnvironment(
            llm_client=llm_client,
            gateway=self._gateway,
            model=model,
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
            max_tool_invocations=self._max_tool_invocations,
            default_gmail_profile=self._default_gmail_profile,
        )
        return self._workflow_executor.run(
            workflow_def,
            environment=env,
            data=data,
            trace=trace,
        )

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
        aux_llm_calls: List[Mapping[str, Any]] = []
        llm_calls: list[dict[str, Any]] = []
        orchestrator_start = time.perf_counter()

        def _record_llm_call(
            *,
            call_type: str,
            model_name: str | None,
            duration_ms: float | None,
            usage: Mapping[str, Any] | None = None,
            note: str | None = None,
        ) -> None:
            payload: dict[str, Any] = {
                "type": call_type,
                "model": model_name,
                "duration_ms": duration_ms,
                "usage": dict(usage) if isinstance(usage, Mapping) else None,
            }
            if isinstance(note, str) and note.strip():
                payload["note"] = note.strip()
            llm_calls.append(payload)

        def _aggregate_usage_total() -> Mapping[str, int] | None:
            totals: dict[str, int] = {}
            any_usage = False
            for call in llm_calls:
                usage_value = call.get("usage")
                if not isinstance(usage_value, dict):
                    continue
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    val = usage_value.get(key)
                    if isinstance(val, int):
                        totals[key] = totals.get(key, 0) + val
                        any_usage = True
            return totals if any_usage else None

        def _orchestrator_duration_ms() -> float:
            return (time.perf_counter() - orchestrator_start) * 1000.0

        def _build_tool_call_parse_error_result(
            tool_call_parse_error: ToolCallParsingError,
            *,
            invocations_override: Sequence[Mapping[str, Any]] = (),
            tool_messages_override: Sequence[Mapping[str, Any]] = (),
        ) -> OrchestratorResult:
            rejected_tool_call = tool_call_parse_error.raw_response
            if isinstance(rejected_tool_call, str):
                rejected_tool_call = rejected_tool_call[:8000]

            tool_invocations = list(invocations_override)
            tool_invocations.append(
                {
                    "tool": "__tool_call_parse_error__",
                    "payload": (
                        {"raw_tool_call": rejected_tool_call}
                        if rejected_tool_call is not None
                        else {}
                    ),
                    "error": str(tool_call_parse_error),
                }
            )

            return OrchestratorResult(
                response_text=(
                    "Tool call was not executed due to an MCP serialisation error. "
                    f"({tool_call_parse_error})\n\n"
                    "Please try again. If this keeps happening, copy the LLM debug output so we can reproduce it."
                ),
                extra_messages=tuple(tool_messages_override),
                tool_invocations=tuple(tool_invocations),
                aux_llm_calls=tuple(aux_llm_calls),
            )

        trace_enabled = os.getenv("VON_WORKFLOWS_TRACE_ENABLED", "0").lower() in {
            "1",
            "true",
        }
        trace = None
        trace_store_fn = None
        if trace_enabled:
            try:
                from ...workflows.trace_model import WorkflowExecutionTrace
                from ...workflows.trace_store import insert_workflow_execution_trace

                trace = WorkflowExecutionTrace(workflow_id="#V#chat_assistant_workflow")
                trace.user_namespace = user_namespace
                trace_store_fn = insert_workflow_execution_trace
            except Exception:
                trace_enabled = False
                trace = None
                trace_store_fn = None

        def _persist_trace(*, status: str, error: str | None = None) -> None:
            if not trace_enabled or trace is None or trace_store_fn is None:
                return
            try:
                if status == "failed" and error:
                    trace.finish_failed(error)
                else:
                    trace.finish_completed()
                stored_execution_id = trace_store_fn(trace.to_storage_document())
                aux_llm_calls.append(
                    {
                        "type": "workflow_execution_trace",
                        "path": "trace",
                        "workflow_id": trace.workflow_id,
                        "execution_id": trace.execution_id,
                        "stored": bool(stored_execution_id),
                        "status": trace.status,
                    }
                )
            except Exception:
                pass

        if not self._gateway.enabled or self._max_tool_invocations <= 0:
            if trace_enabled and trace is not None:
                llm_step = trace.start_step(
                    "llm.generate",
                    inputs={
                        "prompt": prompt,
                        "model": model or "default",
                        "gateway_enabled": bool(self._gateway.enabled),
                        "max_tool_invocations": int(self._max_tool_invocations),
                    },
                )
            llm_start = time.perf_counter()
            response = llm_client.generate(prompt, context=context, model=model)
            _record_llm_call(
                call_type="llm.generate",
                model_name=model,
                duration_ms=(time.perf_counter() - llm_start) * 1000.0,
                usage=None,
                note="Token usage unavailable via legacy generate().",
            )
            if trace_enabled and trace is not None:
                llm_step.finish_success(
                    {
                        "response_preview": (
                            response[:800]
                            if isinstance(response, str)
                            else str(response)[:800]
                        )
                    }
                )
            # If the model emitted a tool-call JSON blob but the gateway is disabled,
            # surface an actionable message instead of returning raw JSON.
            try:
                tool_calls = self._extract_tool_calls(response)
            except ToolCallParsingError:
                tool_calls = None
            if tool_calls:
                tool_name = tool_calls[0].get(self._TOOL_FIELD)
                if isinstance(tool_name, str):
                    result = OrchestratorResult(
                        response_text=(
                            "Internal MCP is disabled, so I couldn't execute the tool call "
                            f"for tool={tool_name!r}. Set VON_INTERNAL_MCP_ENABLE=1 and try again."
                        ),
                        extra_messages=(),
                        tool_invocations=(),
                        aux_llm_calls=(),
                        llm_calls=tuple(llm_calls),
                        llm_usage=_aggregate_usage_total(),
                        orchestrator_duration_ms=_orchestrator_duration_ms(),
                    )
                    _persist_trace(status="completed")
                    return result
            result = OrchestratorResult(
                response_text=response,
                extra_messages=(),
                tool_invocations=(),
                aux_llm_calls=tuple(aux_llm_calls),
                llm_calls=tuple(llm_calls),
                llm_usage=_aggregate_usage_total(),
                orchestrator_duration_ms=_orchestrator_duration_ms(),
            )
            _persist_trace(status="completed")
            return result

        augmented_context = self._build_augmented_context(
            context,
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
        )

        selected_workflow_id = CHAT_ASSISTANT_WORKFLOW_ID
        presenter_mode_requested = False
        if context:
            for msg in context:
                if not isinstance(msg, Mapping):
                    continue
                role = msg.get("role")
                content = msg.get("content")
                if (
                    role == "system"
                    and isinstance(content, str)
                    and content.lstrip().startswith("PRESENTER MODE PROTOCOL:")
                ):
                    presenter_mode_requested = True
                    break

        # Workflow selection is currently only used to decide whether to route to
        # the narration workflow. To avoid interfering with tool-calling flows and
        # test doubles (which often provide a finite response sequence), we only
        # invoke the selector for authenticated presenter-mode turns.
        if (
            user_namespace
            and presenter_mode_requested
            and self._workflow_selector.enabled()
        ):
            try:
                selector_selection = self._workflow_selector.select_workflow(
                    llm_client=llm_client, model=model, turn_text=prompt
                )
                if selector_selection.workflow_id:
                    selected_workflow_id = selector_selection.workflow_id
                aux_llm_calls.append(
                    {
                        "type": "workflow_selector",
                        "workflow_id": selector_selection.workflow_id,
                        "verdict": selector_selection.verdict,
                        "prompt_id": selector_selection.prompt_id,
                    }
                )
                if trace_enabled and trace is not None:
                    trace.metadata["workflow_selector"] = {
                        "workflow_id": selector_selection.workflow_id,
                        "verdict": selector_selection.verdict,
                        "prompt_id": selector_selection.prompt_id,
                    }
            except Exception:
                selected_workflow_id = CHAT_ASSISTANT_WORKFLOW_ID

        def _maybe_apply_narration_routing(screen_text: Any) -> Any:
            if selected_workflow_id != CHAT_NARRATION_WORKFLOW_ID:
                return screen_text

            try:
                screen_value = (
                    screen_text.strip()
                    if isinstance(screen_text, str)
                    else str(screen_text)
                )
                narration_data = {
                    "presenter_mode_requested": True,
                    "force_narration": True,
                    "screen_text": screen_value,
                    "user_prompt": prompt,
                    "presenter_channels": {},
                }

                narration_result = self.execute_workflow(
                    CHAT_NARRATION_WORKFLOW_ID,
                    data=narration_data,
                    llm_client=llm_client,
                    model=model,
                    user_namespace=user_namespace,
                    auxiliary_system_prompt=auxiliary_system_prompt,
                    trace=None,
                )

                channels_obj = (
                    narration_result.data.get("presenter_channels")
                    if narration_result is not None
                    else None
                )
                if isinstance(channels_obj, dict):
                    channels = cast(Mapping[str, Any], channels_obj)
                    spoken = (
                        channels.get("spoken")
                        if isinstance(channels.get("spoken"), str)
                        else None
                    )
                    screen = (
                        channels.get("screen")
                        if isinstance(channels.get("screen"), str)
                        else None
                    )
                    if spoken and screen:
                        aux_llm_calls.append(
                            {
                                "type": "narration",
                                "workflow_id": CHAT_NARRATION_WORKFLOW_ID,
                                "presenter_channels": {
                                    "spoken": spoken,
                                    "screen": screen,
                                },
                            }
                        )
                        return f"<spoken>{spoken}</spoken>\n\n<screen>{screen}</screen>"
            except Exception:
                return screen_text

            return screen_text

        # Phase 3 (JVNAUTOSCI-799): Attempt structured tool calling if available
        use_structured = (
            hasattr(llm_client, "generate_with_tools")
            and hasattr(llm_client, "_should_use_structured_calling")
            and llm_client._should_use_structured_calling()
        )

        if use_structured:
            self._logger.debug("[mcp_orchestrator] Using structured tool calling path")
            try:
                if trace_enabled and trace is not None:
                    llm_step = trace.start_step(
                        "llm.generate_with_tools",
                        inputs={
                            "prompt": prompt,
                            "model": model or "default",
                            "context_messages": len(augmented_context),
                        },
                    )
                tool_definitions = self._convert_mcp_tools_to_structured_definitions()
                llm_start = time.perf_counter()
                llm_response = llm_client.generate_with_tools(
                    prompt=prompt,
                    available_tools=tool_definitions,
                    context=augmented_context,
                    model=model,
                    system_message=None,  # Already in augmented_context
                )
                _record_llm_call(
                    call_type="llm.generate_with_tools",
                    model_name=(
                        llm_response.model
                        if isinstance(getattr(llm_response, "model", None), str)
                        else model
                    ),
                    duration_ms=(time.perf_counter() - llm_start) * 1000.0,
                    usage=(
                        llm_response.usage
                        if isinstance(getattr(llm_response, "usage", None), Mapping)
                        else None
                    ),
                )

                # Convert to legacy format for compatibility
                if llm_response.tool_calls:
                    # Have tool calls - will process via structured path
                    response = llm_response.text_response or ""
                    tool_calls = [
                        {
                            self._ACTION_FIELD: self._CALL_ACTION,
                            self._TOOL_FIELD: tc.tool_name,
                            self._PAYLOAD_FIELD: tc.payload,
                            "_call_id": tc.call_id,  # Preserve for tracing (JVNAUTOSCI-803)
                        }
                        for tc in llm_response.tool_calls
                    ]
                    has_valid_tool_call = True
                    self._logger.debug(
                        "[mcp_orchestrator] Structured calling extracted %d tool(s)",
                        len(tool_calls),
                    )
                else:
                    # No tool calls - return text response
                    response = llm_response.text_response
                    tool_calls = None
                    has_valid_tool_call = False
                if trace_enabled and trace is not None:
                    llm_step.finish_success(
                        {
                            "response_preview": (
                                response[:800]
                                if isinstance(response, str)
                                else str(response)[:800]
                            ),
                            "tool_call_count": len(tool_calls or []),
                        }
                    )
            except Exception as exc:
                if trace_enabled and trace is not None:
                    try:
                        llm_step.finish_failed(str(exc))
                    except Exception:
                        pass
                self._logger.warning(
                    "[mcp_orchestrator] Structured calling failed, falling back to legacy: %s",
                    exc,
                )
                use_structured = False

        if not use_structured:
            # Legacy path: generate() returns text, parse tool calls from JSON
            if trace_enabled and trace is not None:
                llm_step = trace.start_step(
                    "llm.generate",
                    inputs={
                        "prompt": prompt,
                        "model": model or "default",
                        "context_messages": len(augmented_context),
                    },
                )
            llm_start = time.perf_counter()
            response = llm_client.generate(
                prompt, context=augmented_context, model=model
            )
            _record_llm_call(
                call_type="llm.generate",
                model_name=model,
                duration_ms=(time.perf_counter() - llm_start) * 1000.0,
                usage=None,
                note="Token usage unavailable via legacy generate().",
            )
            tool_calls = None  # Will be extracted below
            has_valid_tool_call = False  # Will be set below
            if trace_enabled and trace is not None:
                llm_step.finish_success(
                    {
                        "response_preview": (
                            response[:800]
                            if isinstance(response, str)
                            else str(response)[:800]
                        )
                    }
                )

        # Detect JSON tool-call output for diagnostics (JVNAUTOSCI-698).
        # Only warn if we fail to parse/execute it.
        # Skip detection for structured path - it handles tool calls natively
        tool_call_parse_error: ToolCallParsingError | None = None
        interpretation: _ModelTurnInterpretation | None = None

        if not use_structured:
            interpretation = self._interpret_model_turn(response)
            response = interpretation.response_text
            is_json_action = interpretation.is_json_action
            tool_calls = interpretation.tool_calls
            tool_call_parse_error = interpretation.tool_call_parse_error
            has_valid_tool_call = bool(tool_calls)
        else:
            # Structured path already extracted tool calls above
            is_json_action = False  # Not relevant for structured calling

        # Only attempt extraction if detection passed (has action="call_tool" structure)
        # This prevents treating tool result JSON as tool call requests (JVNAUTOSCI-699)
        if not has_valid_tool_call:
            workflow_def = self._workflow_registry.get(MISSING_TOOL_CALL_WORKFLOW_ID)
            workflow_context = {
                "response_text": (
                    response if isinstance(response, str) else str(response)
                ),
                "interpretation": interpretation,
                "use_structured": use_structured,
                "tool_call_parse_error": tool_call_parse_error,
                "aux_llm_calls": aux_llm_calls,
                "augmented_context": augmented_context,
                "tool_calls": tool_calls,
                "missing_tool_assessor": self._assess_missing_tool_call,
                "extract_tool_calls_fn": self._extract_tool_calls,
                "record_llm_call": _record_llm_call,
            }
            if workflow_def is not None:
                env = WorkflowEnvironment(
                    llm_client=llm_client,
                    gateway=self._gateway,
                    model=model,
                    user_namespace=user_namespace,
                    auxiliary_system_prompt=auxiliary_system_prompt,
                    max_tool_invocations=self._max_tool_invocations,
                    default_gmail_profile=gmail_profile or self._default_gmail_profile,
                )
                workflow_result = self._workflow_executor.run(
                    workflow_def,
                    environment=env,
                    data=workflow_context,
                    trace=trace if trace_enabled else None,
                )
                response = workflow_result.data.get("response_text", response)
                interpretation = workflow_result.data.get(
                    "interpretation", interpretation
                )
                tool_calls = workflow_result.data.get("tool_calls", tool_calls)
                tool_call_parse_error = workflow_result.data.get(
                    "tool_call_parse_error", tool_call_parse_error
                )
                has_valid_tool_call = bool(tool_calls)
                if trace is not None:
                    trace.metadata.setdefault("workflows", {})
                    trace.metadata["workflows"]["missing_tool_call"] = {
                        "final_state": workflow_result.final_state,
                        "completed": workflow_result.completed,
                        "error": workflow_result.error,
                    }

            if not has_valid_tool_call:
                if tool_call_parse_error is not None:
                    result = _build_tool_call_parse_error_result(
                        tool_call_parse_error,
                    )
                    _persist_trace(status="completed")
                    return result

                response_text = _maybe_apply_narration_routing(response)
                result = OrchestratorResult(
                    response_text=response_text,
                    extra_messages=(),
                    tool_invocations=(),
                    aux_llm_calls=tuple(aux_llm_calls),
                    llm_calls=tuple(llm_calls),
                    llm_usage=_aggregate_usage_total(),
                    orchestrator_duration_ms=_orchestrator_duration_ms(),
                )
                _persist_trace(status="completed")
                return result

        assert tool_calls is not None
        self._logger.debug("[mcp_orchestrator] Extracted tool requests: %s", tool_calls)

        invocations: List[Mapping[str, Any]] = []
        tool_messages: List[Mapping[str, Any]] = []
        selected_gmail_profile = gmail_profile or self._default_gmail_profile

        # Support chained tool calls up to max_tool_invocations limit (JVNAUTOSCI-699)
        iteration_count = 0
        current_response = response
        # Track if we're in the first iteration with structured tool calls already extracted
        first_iteration_structured = use_structured and has_valid_tool_call

        while iteration_count < self._max_tool_invocations:
            # Skip extraction on first iteration if structured calling already did it
            if first_iteration_structured:
                first_iteration_structured = False  # Only skip once
            else:
                try:
                    tool_calls = self._extract_tool_calls(current_response)
                except ToolCallParsingError as exc:
                    workflow_def = self._workflow_registry.get(
                        MISSING_TOOL_CALL_WORKFLOW_ID
                    )
                    workflow_context = {
                        "response_text": (
                            current_response
                            if isinstance(current_response, str)
                            else str(current_response)
                        ),
                        "interpretation": None,
                        "use_structured": False,
                        "tool_call_parse_error": exc,
                        "aux_llm_calls": aux_llm_calls,
                        "augmented_context": augmented_context,
                        "tool_calls": None,
                        "missing_tool_assessor": self._assess_missing_tool_call,
                        "extract_tool_calls_fn": self._extract_tool_calls,
                        "record_llm_call": _record_llm_call,
                    }
                    if workflow_def is not None:
                        env = WorkflowEnvironment(
                            llm_client=llm_client,
                            gateway=self._gateway,
                            model=model,
                            user_namespace=user_namespace,
                            auxiliary_system_prompt=auxiliary_system_prompt,
                            max_tool_invocations=self._max_tool_invocations,
                            default_gmail_profile=gmail_profile
                            or self._default_gmail_profile,
                        )
                        workflow_result = self._workflow_executor.run(
                            workflow_def,
                            environment=env,
                            data=workflow_context,
                            trace=trace if trace_enabled else None,
                        )
                        current_response = workflow_result.data.get(
                            "response_text", current_response
                        )
                        tool_calls = workflow_result.data.get("tool_calls")
                        exc = workflow_result.data.get("tool_call_parse_error", exc)
                    if tool_calls:
                        # Resume loop with recovered tool calls
                        pass
                    else:
                        result = _build_tool_call_parse_error_result(
                            exc,
                            invocations_override=tuple(invocations),
                            tool_messages_override=tuple(tool_messages),
                        )
                        _persist_trace(status="completed")
                        return result

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
                augmented_context.extend(
                    [
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": current_response},
                    ]
                )
            else:
                augmented_context.append(
                    {"role": "assistant", "content": current_response}
                )

            for tool_request in tool_calls:
                iteration_count += 1
                tool_name = tool_request.get(self._TOOL_FIELD)
                payload = tool_request.get(self._PAYLOAD_FIELD) or {}
                call_id = tool_request.get("_call_id")

                tool_step = None
                if trace_enabled and trace is not None and isinstance(tool_name, str):
                    tool_step = trace.start_step(
                        f"tool.{tool_name}",
                        inputs={
                            "tool": tool_name,
                            "call_id": call_id,
                            "payload": (
                                dict(payload)
                                if isinstance(payload, Mapping)
                                else payload
                            ),
                        },
                    )

                if not isinstance(tool_name, str):
                    raise ToolCallParsingError(
                        "Tool name must be a string.",
                        raw_response=(
                            current_response
                            if isinstance(current_response, str)
                            else str(current_response)
                        ),
                    )
                if not isinstance(payload, MutableMapping):
                    raise ToolCallParsingError(
                        "Tool payload must be a JSON object.",
                        raw_response=(
                            current_response
                            if isinstance(current_response, str)
                            else str(current_response)
                        ),
                    )

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
                                tool_name,
                            )
                        elif user_namespace:
                            self._logger.info(
                                "[mcp_orchestrator] Tool=%s already has namespace=%s in payload",
                                tool_name,
                                payload.get("namespace"),
                            )
                        else:
                            self._logger.warning(
                                "[mcp_orchestrator] No user_namespace available for tool=%s (unauthenticated request)",
                                tool_name,
                            )
                    else:
                        # Gmail tools must not receive namespace; profile is sufficient for routing
                        if "namespace" in payload:
                            payload.pop("namespace", None)
                            self._logger.info(
                                "[mcp_orchestrator] Removed namespace from Gmail tool=%s payload to satisfy MCP schema",
                                tool_name,
                            )

                    result = self._gateway.invoke(tool_name, payload)
                    tool_payload = self._format_tool_result(
                        tool_name, result.payload, result.duration_ms, "ok"
                    )

                    # Extract call_id for tracing (JVNAUTOSCI-803)
                    invocation_record = {"tool": tool_name, "payload": dict(payload)}
                    if call_id:
                        invocation_record["call_id"] = call_id
                    invocations.append(invocation_record)

                    if tool_step is not None:
                        try:
                            tool_step.finish_success(
                                {
                                    "status": "ok",
                                    "duration_ms": result.duration_ms,
                                }
                            )
                        except Exception:
                            pass

                    log_msg = (
                        "[mcp_orchestrator] Tool invocation #%d: tool=%s, model=%s"
                        + (", call_id=%s" if call_id else "")
                    )
                    log_args = [iteration_count, tool_name, model or "default"]
                    if call_id:
                        log_args.append(call_id)
                    self._logger.info(log_msg, *log_args)
                except (
                    Exception
                ) as exc:  # pragma: no cover - error handling path validated separately
                    tool_payload = self._format_tool_result(
                        tool_name, None, None, "error", str(exc)
                    )

                    # Extract call_id for error tracing (JVNAUTOSCI-803)
                    error_record = {
                        "tool": tool_name,
                        "payload": dict(payload),
                        "error": str(exc),
                    }
                    if call_id:
                        error_record["call_id"] = call_id
                    invocations.append(error_record)

                    if tool_step is not None:
                        try:
                            tool_step.finish_failed(str(exc))
                        except Exception:
                            pass

                    log_msg = "[mcp_orchestrator] Tool %s failed: %s" + (
                        " (call_id=%s)" if call_id else ""
                    )
                    log_args = [tool_name, exc]
                    if call_id:
                        log_args.append(call_id)
                    self._logger.warning(log_msg, *log_args)

                augmented_context.append({"role": "tool", "content": tool_payload})
                tool_messages.append({"role": "tool", "content": tool_payload})

            follow_up_prompt = (
                "Provide a final answer to the user now that the tool result is available. "
                "If the tool failed, explain the error. "
                "If you need to call another tool, you may do so."
            )
            llm_start = time.perf_counter()
            current_response = llm_client.generate(
                follow_up_prompt, context=augmented_context, model=model
            )
            _record_llm_call(
                call_type="llm.generate",
                model_name=model,
                duration_ms=(time.perf_counter() - llm_start) * 1000.0,
                usage=None,
                note="Follow-up after tool execution; token usage unavailable via legacy generate().",
            )

        # Log if we hit the iteration limit
        if (
            iteration_count >= self._max_tool_invocations
            and self._is_json_action_response(current_response)
        ):
            self._logger.warning(
                "[mcp_orchestrator] Reached max tool invocation limit (%d), "
                "but LLM still wants to call tools. Returning current response.",
                self._max_tool_invocations,
            )

        final_response_text = _maybe_apply_narration_routing(current_response)

        result = OrchestratorResult(
            response_text=final_response_text,
            extra_messages=tuple(tool_messages),
            tool_invocations=tuple(invocations),
            aux_llm_calls=tuple(aux_llm_calls),
            llm_calls=tuple(llm_calls),
            llm_usage=_aggregate_usage_total(),
            orchestrator_duration_ms=_orchestrator_duration_ms(),
        )
        _persist_trace(status="completed")
        return result


__all__ = [
    "InternalMCPChatOrchestrator",
    "OrchestratorResult",
    "ToolCallParsingError",
]
