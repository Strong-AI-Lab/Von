"""Chat assistant orchestration utilities for the internal MCP gateway."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import (
    Any,
    cast,
    Callable,
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

from .gateway import InternalMCPGateway, MethodDefinition
from .schemas import Schema as McpSchema, coerce_payload_types, validate_payload

# Structured tool calling support (JVNAUTOSCI-799 Phase 3)
from ...languagemodels.structured_tool_calling import (
    LLMResponse,
    ToolDefinition,
    ToolCall,
)
from src.backend.services.prompt_template_service import PromptTemplateService
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
    WRITE_TOOL_POLICY_WORKFLOW_ID,
    register_default_workflows,
)
from ...workflows.engine import WorkflowExecutor
from ...workflows.workflow_registry import WorkflowRegistry
from ...workflows.workflow_selector import WorkflowSelector

from src.backend.workflows.write_tool_policy import (
    compute_allowed_write_tools,
    prompt_explicitly_denies_write,
)


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
class _ToolCallPreflightResult:
    tool_calls: list[_ToolCallRequest] | None
    errors: list[str]
    warnings: list[str]
    tool_unavailable: list[str]


@dataclass(frozen=True)
class _MissingToolCallDetectorSpec:
    """Declarative definition of the missing-tool-call detector."""

    action_id: str
    prompt_id: Optional[str]
    prompt_text: str
    model: Optional[str]


@dataclass(frozen=True)
class _OntologyPreflightResult:
    message: str | None
    telemetry: Mapping[str, Any] | None


@dataclass(frozen=True)
class _WorkflowModelPolicyState:
    enabled: bool
    policy: Mapping[str, Any] | None
    policy_id: str | None
    predicate_id: str | None
    errors: Sequence[str]


@dataclass(frozen=True)
class _ModelCandidate:
    provider: str | None
    model: str | None
    raw: str
    source: str
    host: str | None = None


class ProgressTracker:
    """Request-scoped progress tracking for the orchestrator.

    JVNAUTOSCI-1038: This class isolates progress state per-request to avoid
    race conditions when multiple requests run concurrently (e.g., multiple
    browser windows generating responses simultaneously).

    Usage:
        tracker = ProgressTracker(callback=my_progress_callback)
        result = orchestrator.run(..., progress_tracker=tracker)

    For background tasks with cancellation support:
        tracker = ProgressTracker(
            callback=my_progress_callback,
            cancellation_checker=lambda: registry.is_cancellation_requested(task_id),
            task_id=task_id,
        )
    """

    # Phase labels mirrored from orchestrator for convenience.
    _PHASE_LABELS: Mapping[str, str] = {
        "context_build": "Building context",
        "tool_plan": "Planning tool calls",
        "tool_execute": "Executing tools",
        "screen_backfill": "Generating response",
        "narration": "Generating narration",
        "completed": "Complete",
        "error": "Error",
        "cancelled": "Cancelled",
    }

    def __init__(
        self,
        callback: Callable[[Mapping[str, Any]], None] | None = None,
        *,
        cancellation_checker: Callable[[], bool] | None = None,
        task_id: str | None = None,
    ) -> None:
        self._callback = callback
        self._cancellation_checker = cancellation_checker
        self._task_id = task_id
        self._start_time: float | None = time.perf_counter() if callback else None
        self._phase_start_time: float | None = None
        self._current_phase: str | None = None
        self._phase_history: list[dict[str, Any]] = []

    @property
    def callback(self) -> Callable[[Mapping[str, Any]], None] | None:
        return self._callback

    @property
    def current_phase(self) -> str | None:
        return self._current_phase

    @property
    def phase_history(self) -> list[dict[str, Any]]:
        return list(self._phase_history)

    @property
    def task_id(self) -> str | None:
        return self._task_id

    def is_cancellation_requested(self) -> bool:
        """Check if cancellation was requested (non-raising)."""
        if self._cancellation_checker is None:
            return False
        try:
            return self._cancellation_checker()
        except Exception:
            return False

    def check_cancellation(self) -> None:
        """Check for cancellation and raise CancellationRequested if requested.

        Call this at safe points in long-running operations to allow graceful
        termination when a user cancels a background task.

        Raises:
            CancellationRequested: If cancellation has been requested.
        """
        if self.is_cancellation_requested():
            self.transition_phase("cancelled")
            raise CancellationRequested(task_id=self._task_id)

    def emit(self, info: Mapping[str, Any]) -> None:
        """Emit a progress event to the callback (if set)."""
        if self._callback is None:
            return
        try:
            enriched: dict[str, Any] = dict(info)
            if self._current_phase:
                enriched.setdefault("phase", self._current_phase)
                enriched.setdefault(
                    "phase_label",
                    self._PHASE_LABELS.get(self._current_phase, ""),
                )
            if self._start_time is not None:
                enriched["total_elapsed_ms"] = int(
                    (time.perf_counter() - self._start_time) * 1000
                )
            if self._phase_start_time is not None:
                enriched["phase_elapsed_ms"] = int(
                    (time.perf_counter() - self._phase_start_time) * 1000
                )
            self._callback(enriched)
        except Exception:
            # Progress is best-effort; never disrupt the main chat flow.
            pass

    def transition_phase(
        self,
        phase: str,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """Transition to a new phase and emit the transition event."""
        now = time.perf_counter()

        # Record previous phase in history.
        if self._current_phase and self._phase_start_time is not None:
            self._phase_history.append(
                {
                    "phase": self._current_phase,
                    "phase_label": self._PHASE_LABELS.get(self._current_phase, ""),
                    "duration_ms": int((now - self._phase_start_time) * 1000),
                }
            )

        # Update current phase.
        self._current_phase = phase
        self._phase_start_time = now

        # Emit the transition.
        info: dict[str, Any] = {
            "status": "phase_transition",
            "phase": phase,
            "phase_label": self._PHASE_LABELS.get(phase, ""),
        }
        if extra:
            info.update(extra)

        self.emit(info)


class ToolCallParsingError(Exception):
    """Raised when a model returns an invalid tool call payload."""

    def __init__(self, message: str, *, raw_response: str | None = None) -> None:
        super().__init__(message)
        self.raw_response = raw_response


class CancellationRequested(Exception):
    """Raised when a background task cancellation is requested.

    JVNAUTOSCI-1038: This exception is raised by ProgressTracker.check_cancellation()
    when the associated background task has been flagged for cancellation.
    The orchestrator catches this to gracefully terminate long-running operations.
    """

    def __init__(self, task_id: str | None = None) -> None:
        self.task_id = task_id
        super().__init__(f"Cancellation requested for task {task_id or 'unknown'}")


class InternalMCPChatOrchestrator:
    """Simple loop that allows the Von assistant to call internal tools."""

    _ACTION_FIELD = "action"
    _CALL_ACTION = "call_tool"
    _TOOL_FIELD = "tool"
    _PAYLOAD_FIELD = "payload"
    _BASE_SYSTEM_PROMPT_TYPE_ID = "#V#von_chat_base_system_prompt"
    _CURRENT_BASE_SYSTEM_PROMPT_TYPE_ID = "#V#current_von_base_system_prompt"
    _MISSING_TOOL_CALL_ACTION_ID = "#V#detect_missing_tool_call_action"
    _PREFLIGHT_PREDICATE_TYPE_ID = "#V#conversation_preflight_predicate"
    _PREFLIGHT_CACHE_TTL_SECONDS = 120
    _TOOL_CALL_REPAIR_PROMPT = (
        "You are a strict tool-call repairer for an MCP agent.\n"
        "Return ONLY a JSON object or JSON array of tool-call objects.\n"
        "Do NOT include prose, markdown fences, or explanations.\n\n"
        "Each tool call must follow:\n"
        '{{"action": "call_tool", "tool": "tool_name", "payload": {{"param": "value"}}}}\n\n'
        "Constraints:\n"
        "- Use only tools that appear in the available tool list.\n"
        "- Ensure payload types match the schema (e.g. integers are integers).\n"
        "- If you cannot produce a valid tool call, return an empty JSON array [].\n\n"
        "Available tools:\n"
        "{tool_list}\n\n"
        "Validation errors to fix:\n"
        "{errors}\n\n"
        "Original tool-call JSON:\n"
        "{raw_tool_call}\n"
    )
    _TOOL_CALL_REPAIR_PROMPTS = ("#V#tool_call_repair_prompt",)

    # --- Tool-use progress phases (JVNAUTOSCI-984) ---
    # Used to provide phase-aware status updates during tool execution.
    PHASE_CONTEXT_BUILD = "context_build"
    PHASE_TOOL_PLAN = "tool_plan"
    PHASE_TOOL_EXECUTE = "tool_execute"
    PHASE_SCREEN_BACKFILL = "screen_backfill"
    PHASE_NARRATION = "narration"
    PHASE_COMPLETED = "completed"
    PHASE_ERROR = "error"

    _PHASE_LABELS: Mapping[str, str] = {
        PHASE_CONTEXT_BUILD: "Building context",
        PHASE_TOOL_PLAN: "Planning tool calls",
        PHASE_TOOL_EXECUTE: "Executing tools",
        PHASE_SCREEN_BACKFILL: "Generating response",
        PHASE_NARRATION: "Generating narration",
        PHASE_COMPLETED: "Complete",
        PHASE_ERROR: "Error",
    }

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
        tool_batch_cap: int = 4,
        default_gmail_profile: str | None = None,
        max_context_chars: int | None = None,
        max_tool_result_chars: int | None = None,
        max_tool_result_field_chars: int | None = None,
    ) -> None:
        self._gateway = gateway
        self._logger = logger or logging.getLogger(__name__)
        self._max_tool_invocations = max(0, int(max_tool_invocations))
        self._tool_batch_cap = max(1, int(tool_batch_cap))
        self._default_gmail_profile = default_gmail_profile

        # Optional progress callback for UI telemetry (JVNAUTOSCI-942).
        # This must never be allowed to break tool execution.
        self._progress_callback: Callable[[Mapping[str, Any]], None] | None = None

        # Phase tracking for tool-use progress (JVNAUTOSCI-984).
        self._progress_start_time: float | None = None
        self._progress_phase_start_time: float | None = None
        self._progress_current_phase: str | None = None
        self._progress_phase_history: list[dict[str, Any]] = []

        # Vontology-backed missing-tool-call detector (lazy loaded)
        self._missing_tool_call_detector: Optional[_MissingToolCallDetectorSpec] = None
        self._missing_tool_call_detector_loaded: bool = False

        # Deterministic ontology preflight cache (per language).
        self._preflight_cache: dict[str, dict[str, Any]] = {}

        # Workflow model policy cache (single policy instance).
        self._workflow_model_policy_cache: dict[str, Any] = {}
        self._workflow_model_policy_cache_ttl_seconds = self._coerce_int(
            None,
            env_var="VON_WORKFLOW_MODEL_POLICY_CACHE_SECONDS",
            default=120,
            min_value=10,
            max_value=3600,
        )

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
        self._last_base_system_prompt_telemetry: dict[str, Any] | None = None

    def configure_execution_caps(
        self,
        *,
        max_tool_invocations: int | None = None,
        tool_batch_cap: int | None = None,
    ) -> None:
        """Update execution caps in-place.

        This is intentionally lightweight so callers (e.g., /von/generate) can
        apply persisted Settings without requiring a server restart.
        """

        if max_tool_invocations is not None:
            try:
                coerced = int(max_tool_invocations)
            except Exception:
                coerced = self._max_tool_invocations
            # Safety clamp.
            self._max_tool_invocations = max(0, min(50, coerced))

        if tool_batch_cap is not None:
            try:
                coerced = int(tool_batch_cap)
            except Exception:
                coerced = self._tool_batch_cap
            self._tool_batch_cap = max(1, min(50, coerced))

    def get_execution_caps(self) -> dict[str, int]:
        return {
            "max_tool_invocations": int(self._max_tool_invocations),
            "tool_batch_cap": int(self._tool_batch_cap),
        }

    def set_progress_callback(
        self, callback: Callable[[Mapping[str, Any]], None] | None
    ) -> None:
        """Set or clear the instance-level progress callback.

        .. deprecated:: JVNAUTOSCI-1038
            Use ``progress_tracker`` parameter in ``run()`` instead to avoid
            race conditions with concurrent requests.
        """
        self._progress_callback = callback
        # Reset phase tracking when a new callback is set (new request).
        self._progress_start_time = time.perf_counter() if callback else None
        self._progress_phase_start_time = None
        self._progress_current_phase = None
        self._progress_phase_history = []

    def _emit_progress(self, info: Mapping[str, Any]) -> None:
        callback = getattr(self, "_progress_callback", None)
        if callback is None:
            return
        try:
            # Enrich progress with phase and timing info (JVNAUTOSCI-984).
            enriched: dict[str, Any] = dict(info)
            if self._progress_current_phase:
                enriched.setdefault("phase", self._progress_current_phase)
                enriched.setdefault(
                    "phase_label",
                    self._PHASE_LABELS.get(self._progress_current_phase, ""),
                )
            if self._progress_start_time is not None:
                enriched["total_elapsed_ms"] = int(
                    (time.perf_counter() - self._progress_start_time) * 1000
                )
            if self._progress_phase_start_time is not None:
                enriched["phase_elapsed_ms"] = int(
                    (time.perf_counter() - self._progress_phase_start_time) * 1000
                )
            callback(enriched)
        except Exception:
            # Progress is best-effort; never disrupt the main chat flow.
            return

    def _emit_phase_transition(
        self,
        phase: str,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """Emit a phase transition event and update internal tracking.

        Call this at the start of each major processing phase to provide
        clear status updates to the UI (JVNAUTOSCI-984).
        """
        now = time.perf_counter()

        # Record previous phase in history.
        if self._progress_current_phase and self._progress_phase_start_time is not None:
            self._progress_phase_history.append(
                {
                    "phase": self._progress_current_phase,
                    "phase_label": self._PHASE_LABELS.get(
                        self._progress_current_phase, ""
                    ),
                    "duration_ms": int((now - self._progress_phase_start_time) * 1000),
                }
            )

        # Update current phase.
        self._progress_current_phase = phase
        self._progress_phase_start_time = now

        # Emit the transition.
        info: dict[str, Any] = {
            "status": "phase_transition",
            "phase": phase,
            "phase_label": self._PHASE_LABELS.get(phase, ""),
        }
        if extra:
            info.update(extra)

        self._emit_progress(info)

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

        registry.register(
            ActionSpec(
                action_id="write_policy.decide",
                handler=self._action_write_policy_decide,
                description="Decide which write-category tools are allowed for this prompt.",
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
                classifier_model=data.get("classifier_model"),
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

    def _action_write_policy_decide(self, request: Any) -> WorkflowActionResult:
        prompt = request.data.get("prompt")
        requested_tools = request.data.get("requested_write_tools")
        recent_user_prompts = request.data.get("recent_user_prompts")
        if not isinstance(prompt, str):
            prompt = ""
        if not isinstance(requested_tools, list):
            requested_tools = []
        if not isinstance(recent_user_prompts, list):
            recent_user_prompts = []

        # Admin override: allow all requested write tools.
        try:
            from src.backend.services.settings_service import (
                get_disable_write_tool_conservatism,
            )

            if get_disable_write_tool_conservatism():
                if not prompt_explicitly_denies_write(prompt):
                    allowed = sorted({str(tool) for tool in requested_tools if tool})
                    return WorkflowActionResult(
                        outputs={
                            "allowed_write_tools": allowed,
                            "write_policy_reason": "write_conservatism_disabled_by_admin_setting",
                        }
                    )
        except Exception:
            # Defensive: do not fail policy evaluation if Settings storage is unavailable.
            pass

        decision = compute_allowed_write_tools(
            prompt=prompt,
            requested_tools=[str(tool) for tool in requested_tools if tool],
            recent_user_prompts=[
                str(item) for item in recent_user_prompts if isinstance(item, str)
            ],
        )

        return WorkflowActionResult(
            outputs={
                "allowed_write_tools": sorted(decision.allowed_tools),
                "write_policy_reason": decision.reason,
            }
        )

    def _action_missing_tool_call_retry(self, request: Any) -> WorkflowActionResult:
        data = request.data
        aux_log = data.setdefault("aux_llm_calls", [])
        augmented_context = data.get("augmented_context") or []
        record_llm_call = data.get("record_llm_call")

        forced = self._infer_missing_tool_call_retry_tool_calls(
            augmented_context,
            user_prompt=data.get("user_prompt"),
        )
        if forced:
            import json

            try:
                aux_log.append(
                    {
                        "type": "missing_tool_call_retry",
                        "path": "forced",
                        "mechanism": "heuristic",
                        "stage": "response",
                        "retry_reason": data.get("missing_tool_call_retry_reason")
                        or "",
                        "response_preview": "(forced tool call)",
                    }
                )
            except Exception:
                pass

            response_text = (
                json.dumps(forced[0]) if len(forced) == 1 else json.dumps(forced)
            )

            return WorkflowActionResult(
                outputs={
                    "response_text": response_text,
                    "tool_calls": forced,
                    "missing_tool_call_retry_success": True,
                    "tool_call_parse_error": None,
                },
                duration_ms=0.0,
            )

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

        policy_state = data.get("policy_state")
        default_model = data.get("default_model")
        user_concept_id = data.get("user_concept_id")
        org_concept_id = data.get("org_concept_id")
        registry_snapshot = data.get("registry_snapshot")

        retry_response = None
        duration_ms = 0.0

        if isinstance(policy_state, _WorkflowModelPolicyState) and callable(
            record_llm_call
        ):
            llm_start = time.perf_counter()
            retry_response, _, _ = self._run_llm_with_fallbacks(
                stage="tool_recovery",
                prompt=prompt_text,
                context=augmented_context,
                default_client=request.environment.llm_client,
                default_model=default_model or request.environment.model,
                policy_state=policy_state,
                registry_snapshot=(
                    registry_snapshot
                    if isinstance(registry_snapshot, Mapping)
                    else None
                ),
                user_concept_id=(
                    user_concept_id
                    if isinstance(user_concept_id, str)
                    else request.environment.user_namespace
                ),
                org_concept_id=(
                    org_concept_id if isinstance(org_concept_id, str) else None
                ),
                llm_calls_log=data.get("llm_calls_log") or [],
                aux_log=aux_log,
                record_llm_call=record_llm_call,
            )
            duration_ms = (time.perf_counter() - llm_start) * 1000.0
        else:
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
                    stage="tool_recovery",
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
            else:
                # Fall back to the last whitespace so we don't chop words.
                cut = clipped.rfind(" ")
                if cut > 200:
                    clipped = clipped[:cut]
            raw = clipped.strip()
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

        timing_hint = self._build_client_capabilities_narration_timing_hint()
        if timing_hint:
            narration_system += "\n" + timing_hint
        if prompt_text:
            narration_system += "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
            narration_system += prompt_text

        narration_user = (
            "User message:\n"
            f"{user_prompt}\n\n"
            "On-screen content (do not read verbatim if long; summarise):\n"
            f"{screen_text}\n"
        )

        policy_state = request.data.get("policy_state")
        default_model = request.data.get("default_model")
        record_llm_call = request.data.get("record_llm_call")
        aux_llm_calls = request.data.get("aux_llm_calls")
        user_concept_id = request.data.get("user_concept_id")
        org_concept_id = request.data.get("org_concept_id")
        registry_snapshot = request.data.get("registry_snapshot")

        if (
            isinstance(policy_state, _WorkflowModelPolicyState)
            and callable(record_llm_call)
            and isinstance(aux_llm_calls, list)
        ):
            narration_response, _, _ = self._run_llm_with_fallbacks(
                stage="narration",
                prompt="Generate <spoken> talk track",
                context=[
                    {"role": "system", "content": narration_system},
                    {"role": "user", "content": narration_user},
                ],
                default_client=request.environment.llm_client,
                default_model=default_model or request.environment.model,
                policy_state=policy_state,
                registry_snapshot=(
                    registry_snapshot
                    if isinstance(registry_snapshot, Mapping)
                    else None
                ),
                user_concept_id=(
                    user_concept_id
                    if isinstance(user_concept_id, str)
                    else request.environment.user_namespace
                ),
                org_concept_id=(
                    org_concept_id if isinstance(org_concept_id, str) else None
                ),
                llm_calls_log=request.data.get("llm_calls_log") or [],
                aux_log=aux_llm_calls,
                record_llm_call=record_llm_call,
            )
        else:
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
        allow_unknown = mcp_schema.get("allow_unknown")

        properties: Dict[str, Any] = {}
        required_list: List[str] = []

        # Process required fields (accept dict or list)
        if isinstance(required_fields, dict):
            for field_name, field_type in required_fields.items():
                properties[field_name] = {
                    "type": self._python_type_to_json_schema_type(field_type)
                }
                required_list.append(field_name)
        elif isinstance(required_fields, list):
            for field_name in required_fields:
                if not isinstance(field_name, str):
                    continue
                properties[field_name] = {"type": "string"}
                required_list.append(field_name)

        # Process optional fields (accept dict or list)
        if isinstance(optional_fields, dict):
            for field_name, field_type in optional_fields.items():
                properties[field_name] = {
                    "type": self._python_type_to_json_schema_type(field_type)
                }
        elif isinstance(optional_fields, list):
            for field_name in optional_fields:
                if not isinstance(field_name, str):
                    continue
                if field_name not in properties:
                    properties[field_name] = {"type": "string"}

        json_schema = {
            "type": "object",
            "properties": properties,
        }

        if allow_unknown is True:
            json_schema["additionalProperties"] = True

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
        preferred_language: str | None = None,
    ) -> str:
        """Build system instruction emphasizing immediate tool invocation behaviour.

        Design rationale (JVNAUTOSCI-698): Focus on BEHAVIOUR (invoke immediately)
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

        max_invocations = int(getattr(self, "_max_tool_invocations", 0))
        batch_cap = int(getattr(self, "_tool_batch_cap", 4))

        base_message, base_prompt_concept_id = (
            self._load_base_system_prompt_from_vontology(
                preferred_language=preferred_language
            )
        )
        if not base_message:
            base_message = (
                "You have access to internal MCP tools.\n\n"
                "{auth_status}\n"
                "⚠️ WHEN TO USE TOOLS (CHECK THESE FIRST) ⚠️\n"
                "If user asks for RECENT, CURRENT, LATEST, NEW, or BREAKING information → USE search_web\n"
                'If user mentions specific dates (2024+, 2025+, "this year", "this month") → USE search_web\n'
                'If user explicitly says "search", "look up", "find information on" → USE search_web\n'
                'If user asks "what\'s new", "recent developments", "latest research" → USE search_web\n'
                "If user provides a URL to analyse or extract content from → USE extract_url (or resilient_extract_url for JS-heavy/blocked pages)\n"
                "If user asks a direct factual question needing verification → USE qna_search\n"
                "If searching within specific domain/context (e.g., site:example.com) → USE context_search\n"
                "ARXIV TOOL ROUTING:\n"
                "- To search arXiv by author/topic/keywords → USE search_arxiv\n"
                "- To list papers already cached locally / already stored → USE list_papers\n"
                "- To download a specific arXiv PDF and store it durably → USE download_paper (requires arxiv_id)\n"
                "- To upload a PDF that is already cached and register a file-copy record → USE finalise_cached_paper (requires arxiv_id)\n"
                "- To read/summarise an already-downloaded paper → USE read_paper (requires arxiv_id)\n"
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
                "- You MAY batch multiple tool calls in ONE message as a JSON array (keep it to <= {batch_cap} calls)\n"
                "- Server limits: max tool invocations per turn = {max_tool_invocations}; tool calls per batch = {batch_cap}\n"
                "- After you receive the tool result (role 'tool'), respond naturally to the user\n\n"
                "VONTOLOGY KINDS & PREDICATES (MUST FOLLOW):\n"
                "- Predicates are a distinct logical kind. A usable predicate MUST satisfy: is_an_instance_of #V#predicate (or a predicate subtype).\n"
                "- Do NOT treat predicates as types or individuals. Predicatehood is NOT inferred from is_a_type_of.\n"
                "- Types are defined by is_a_type_of. A concept can also be is_an_instance_of #V#type, but that does NOT make it an individual.\n"
                "- If a concept is NOT a predicate, NOT a type, and DOES have is_an_instance_of, then it is an individual.\n"
                "- Before using a predicate in add_relationship: fetch_concept(predicate_id) and confirm is_an_instance_of includes #V#predicate (or subtype).\n\n"
                "VERIFICATION & CONSISTENCY RULES:\n"
                "- If the user doubts whether a specific concept_id exists (e.g. '#V#...') or challenges a claim about Vontology state, ALWAYS verify first using fetch_concept (or search_concepts) before responding.\n"
                "- Do NOT reply with prose-only 'we should verify' / 'I will not assert unless I can verify' without actually calling a tool.\n\n"
                "If you output JSON, the system will execute that tool call immediately.\n\n"
                "IMPORTANT: arXiv paper conversions (PDF to markdown) can take 5-10 minutes.\n"
                "If read_paper fails, the paper may still be converting. Check with list_papers.\n\n"
                "Available tools:\n"
                "{listing}"
            )

        base_message = self._inject_prompt_variable(
            base_message, key="auth_status", value=auth_status.strip() or auth_status
        )
        base_message = self._inject_prompt_variable(
            base_message, key="batch_cap", value=str(batch_cap)
        )
        base_message = self._inject_prompt_variable(
            base_message, key="max_tool_invocations", value=str(max_invocations)
        )
        base_message = self._inject_prompt_variable(
            base_message, key="listing", value=listing
        )

        self._last_base_system_prompt_telemetry = {
            "type": "base_system_prompt",
            "source": "vontology" if base_prompt_concept_id else "code_fallback",
            "prompt_type_id": self._BASE_SYSTEM_PROMPT_TYPE_ID,
            "prompt_concept_id": base_prompt_concept_id or "",
        }

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

    def _load_base_system_prompt_from_vontology(
        self, *, preferred_language: str | None = None
    ) -> tuple[str | None, str | None]:
        """Load the base system prompt from Vontology (best effort)."""
        try:
            from src.backend.services.concept_search_service import search_concepts
        except Exception:
            return None, None

        try:
            from src.backend.services.text_value_service import get_texts_for_concept
        except Exception:
            return None, None

        try:
            from src.backend.services.concept_service import (
                get_concept_by_concept_id,
                update_concept,
            )
        except Exception:
            get_concept_by_concept_id = None  # type: ignore[assignment]
            update_concept = None  # type: ignore[assignment]

        try:
            from bson import ObjectId
        except Exception:
            ObjectId = None  # type: ignore[assignment]

        def _normalise_instances(value: Any) -> list[str]:
            if isinstance(value, str):
                return [value] if value else []
            if isinstance(value, list):
                return [item for item in value if isinstance(item, str) and item]
            return []

        def _is_instance_of(concept: Mapping[str, Any], target_id: str) -> bool:
            relationships = (
                concept.get("relationships") if isinstance(concept, Mapping) else None
            )
            if not isinstance(relationships, Mapping):
                return False
            return target_id in _normalise_instances(
                relationships.get("is_an_instance_of")
            )

        def _select_prompt_for_concept(
            concept_id: str,
        ) -> tuple[str | None, tuple[int, str] | None]:
            try:
                texts = get_texts_for_concept(concept_id, limit=80)
            except Exception:
                texts = []

            if not isinstance(texts, list) or not texts:
                return None, None

            candidates: list[dict[str, Any]] = []
            for text in texts:
                if not isinstance(text, dict):
                    continue
                predicate = text.get("predicate")
                if predicate not in {"hasContent", "hasDescription"}:
                    continue
                text_value = text.get("text")
                if not isinstance(text_value, str) or not text_value.strip():
                    continue
                candidates.append(text)

            if not candidates:
                return None, None

            def _type_rank(pred: str | None) -> int:
                return 0 if pred == "hasContent" else 1

            def _lang_rank(lang: str | None) -> int:
                if not variants or not isinstance(lang, str):
                    return 2
                if lang == variants[0]:
                    return 0
                if len(variants) > 1 and lang == variants[1]:
                    return 1
                return 2

            candidates.sort(
                key=lambda item: (
                    _type_rank(item.get("predicate")),
                    _lang_rank(item.get("lang")),
                    str(item.get("text_value_id") or ""),
                )
            )

            prompt_text = candidates[0].get("text")
            if not isinstance(prompt_text, str) or not prompt_text.strip():
                return None, None

            latest_ts = 0
            latest_id = ""
            for item in candidates:
                text_id = item.get("text_value_id")
                text_id_str = str(text_id) if text_id is not None else ""
                ts = 0
                if ObjectId is not None:
                    try:
                        if text_id:
                            ts = int(ObjectId(str(text_id)).generation_time.timestamp())
                    except Exception:
                        ts = 0
                if ts > latest_ts:
                    latest_ts = ts
                    latest_id = text_id_str

            return prompt_text.strip(), (latest_ts, latest_id)

        def _sync_current_prompt_instance(
            selected_id: str,
            current_ids: list[str],
        ) -> None:
            if not selected_id or not update_concept or not get_concept_by_concept_id:
                return
            current_type_id = self._CURRENT_BASE_SYSTEM_PROMPT_TYPE_ID

            try:
                current_type_doc = get_concept_by_concept_id(current_type_id)
            except Exception:
                current_type_doc = None
            if not isinstance(current_type_doc, Mapping):
                return

            for cid in current_ids:
                if not isinstance(cid, str) or not cid or cid == selected_id:
                    continue
                try:
                    concept_doc = get_concept_by_concept_id(cid)
                except Exception:
                    concept_doc = None
                if not isinstance(concept_doc, Mapping):
                    continue
                inst = _normalise_instances(
                    (concept_doc.get("relationships") or {}).get("is_an_instance_of")
                )
                if current_type_id in inst:
                    inst = [item for item in inst if item != current_type_id]
                    try:
                        update_concept(
                            cid,
                            {"relationships.is_an_instance_of": inst},
                        )
                    except Exception:
                        continue

            try:
                selected_doc = get_concept_by_concept_id(selected_id)
            except Exception:
                selected_doc = None
            if not isinstance(selected_doc, Mapping):
                return
            selected_inst = _normalise_instances(
                (selected_doc.get("relationships") or {}).get("is_an_instance_of")
            )
            if current_type_id not in selected_inst:
                selected_inst.append(current_type_id)
                try:
                    update_concept(
                        selected_id,
                        {"relationships.is_an_instance_of": selected_inst},
                    )
                except Exception:
                    return

        try:
            current_result = search_concepts(
                query="",
                instance_of=self._CURRENT_BASE_SYSTEM_PROMPT_TYPE_ID,
                limit=50,
                match_type="all",
            )
        except Exception:
            current_result = None

        current_entries = (
            current_result.get("results")
            if isinstance(current_result, Mapping)
            else None
        )
        if not isinstance(current_entries, list):
            current_entries = []

        preferred_language = self._normalise_language(preferred_language)
        variants = self._language_variants(preferred_language)

        current_candidates: list[str] = []
        if current_entries and get_concept_by_concept_id:
            for entry in current_entries:
                if not isinstance(entry, Mapping):
                    continue
                concept_id = entry.get("concept_id")
                if not isinstance(concept_id, str) or not concept_id:
                    continue
                try:
                    concept_doc = get_concept_by_concept_id(concept_id)
                except Exception:
                    concept_doc = None
                if not isinstance(concept_doc, Mapping):
                    continue
                if _is_instance_of(concept_doc, self._BASE_SYSTEM_PROMPT_TYPE_ID):
                    current_candidates.append(concept_id)

        if len(current_candidates) == 1:
            prompt_text, _ = _select_prompt_for_concept(current_candidates[0])
            if prompt_text:
                return prompt_text, current_candidates[0]

        try:
            result = search_concepts(
                query="",
                instance_of=self._BASE_SYSTEM_PROMPT_TYPE_ID,
                limit=50,
                match_type="all",
            )
        except Exception:
            return None, None

        entries = result.get("results") if isinstance(result, Mapping) else None
        if not isinstance(entries, list):
            return None, None

        best_prompt: str | None = None
        best_timestamp: tuple[int, str] | None = None
        best_concept_id: str | None = None

        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            concept_id = entry.get("concept_id")
            if not isinstance(concept_id, str) or not concept_id:
                continue

            prompt_text, timestamp_key = _select_prompt_for_concept(concept_id)
            if not prompt_text or not timestamp_key:
                continue

            if best_timestamp is None or timestamp_key > best_timestamp:
                best_timestamp = timestamp_key
                best_prompt = prompt_text
                best_concept_id = concept_id

        if best_prompt and best_concept_id:
            current_ids: list[str] = []
            for entry in current_entries:
                if not isinstance(entry, Mapping):
                    continue
                concept_id = entry.get("concept_id")
                if isinstance(concept_id, str) and concept_id:
                    current_ids.append(concept_id)
            _sync_current_prompt_instance(best_concept_id, current_ids)

        return best_prompt, best_concept_id

    def _consume_base_system_prompt_telemetry(self) -> dict[str, Any] | None:
        telemetry = self._last_base_system_prompt_telemetry
        self._last_base_system_prompt_telemetry = None
        return telemetry

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

    @staticmethod
    def _parse_policy_model_candidate(value: Any) -> _ModelCandidate | None:
        if not isinstance(value, str):
            return None

        raw = value.strip()
        if not raw:
            return None

        if raw == "active_llm":
            return _ModelCandidate(
                provider=None,
                model=None,
                raw=raw,
                source="active_llm",
                host=None,
            )

        candidate = raw
        if candidate.startswith("#V#"):
            candidate = candidate[3:]

        provider = None
        model = candidate
        host = None

        if "://" in candidate:
            provider = "ollama"
            host, _, model = candidate.partition("://")
            model = model.strip()
            host = host.strip()
        elif ":" in candidate:
            provider, _, model = candidate.partition(":")
            provider = provider.strip().lower() or None
            model = model.strip()

        if not model:
            return None

        return _ModelCandidate(
            provider=provider,
            model=model,
            raw=raw,
            source="policy",
            host=host,
        )

    @staticmethod
    def _resolve_registry_model_candidate(
        value: Any,
        registry_snapshot: Mapping[str, Any] | None,
    ) -> Any:
        if not isinstance(value, str):
            return value

        if not registry_snapshot:
            return value

        models = registry_snapshot.get("models")
        if not isinstance(models, list):
            return value

        raw = value.strip()
        if not raw:
            return value

        for entry in models:
            if not isinstance(entry, Mapping):
                continue
            if raw in {
                entry.get("concept_id"),
                entry.get("registry_entry_id"),
            }:
                model_id = entry.get("model_id")
                if isinstance(model_id, str):
                    model_id = model_id.strip()
                else:
                    model_id = ""
                provider = entry.get("provider")
                if (
                    model_id
                    and isinstance(provider, str)
                    and provider.strip()
                    and ":" not in model_id
                ):
                    model_id = f"{provider.strip().lower()}:{model_id}"
                return model_id or value

        return value

    def _stage_model_candidates(
        self,
        *,
        stage: str,
        default_model: Optional[str],
        policy_state: _WorkflowModelPolicyState,
        registry_snapshot: Mapping[str, Any] | None = None,
    ) -> list[_ModelCandidate]:
        candidates: list[_ModelCandidate] = []

        if not policy_state.enabled or not policy_state.policy:
            return [
                _ModelCandidate(
                    provider=None,
                    model=default_model,
                    raw="active_llm",
                    source="active_llm",
                    host=None,
                )
            ]

        stages = None
        if policy_state.policy and isinstance(
            policy_state.policy.get("stages"), Mapping
        ):
            stages = policy_state.policy.get("stages")

        stage_config = None
        if isinstance(stages, Mapping):
            stage_config = stages.get(stage)

        primary = None
        fallback = None
        if isinstance(stage_config, Mapping):
            primary = stage_config.get("primary")
            fallback = stage_config.get("fallback")

        primary = self._resolve_registry_model_candidate(primary, registry_snapshot)
        fallback = (
            [
                self._resolve_registry_model_candidate(item, registry_snapshot)
                for item in fallback
            ]
            if isinstance(fallback, list)
            else fallback
        )

        primary_candidate = self._parse_policy_model_candidate(primary)
        if primary_candidate:
            candidates.append(primary_candidate)

        if isinstance(fallback, list):
            for item in fallback:
                fallback_candidate = self._parse_policy_model_candidate(item)
                if fallback_candidate:
                    candidates.append(fallback_candidate)

        # Always include active LLM as last resort.
        candidates.append(
            _ModelCandidate(
                provider=None,
                model=default_model,
                raw="active_llm",
                source="active_llm",
                host=None,
            )
        )

        # De-duplicate by provider+model+host+source
        seen: set[tuple[str | None, str | None, str | None, str]] = set()
        unique: list[_ModelCandidate] = []
        for candidate in candidates:
            key = (
                candidate.provider,
                candidate.model,
                candidate.host,
                candidate.source,
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)

        return unique

    @staticmethod
    def _is_workflow_model_policy_enabled() -> bool:
        return os.getenv("VON_WORKFLOW_MODEL_POLICY_ENABLE", "0").lower() in {
            "1",
            "true",
        }

    def _resolve_concept_id_by_name(
        self,
        name: str,
        *,
        preferred_language: str | None = None,
    ) -> Optional[str]:
        if not isinstance(name, str) or not name.strip():
            return None
        try:
            from src.backend.services.concept_resolution_service import (
                resolve_concept_by_name,
            )
        except Exception:
            return None

        try:
            resolution = resolve_concept_by_name(
                name=name.strip(),
                preferred_languages=(
                    [preferred_language] if preferred_language else None
                ),
                match_code_strings=True,
            )
        except Exception:
            return None

        if not isinstance(resolution, Mapping):
            return None

        if resolution.get("status") != "resolved":
            return None

        resolved = resolution.get("resolved_concept_id")
        return resolved if isinstance(resolved, str) else None

    def _load_workflow_model_policy(
        self, preferred_language: str | None
    ) -> tuple[_WorkflowModelPolicyState, Mapping[str, Any] | None]:
        now = time.time()
        cached = self._workflow_model_policy_cache
        if cached:
            expires_at = cached.get("expires_at")
            state = cached.get("state")
            telemetry_value = cached.get("telemetry")
            if (
                isinstance(expires_at, (int, float))
                and expires_at > now
                and isinstance(state, _WorkflowModelPolicyState)
            ):
                cached_telemetry = (
                    telemetry_value if isinstance(telemetry_value, Mapping) else None
                )
                return state, cached_telemetry

        enabled = self._is_workflow_model_policy_enabled()
        policy_name = os.getenv(
            "VON_WORKFLOW_MODEL_POLICY_NAME", "default_workflow_model_policy"
        )
        predicate_name = os.getenv(
            "VON_WORKFLOW_MODEL_POLICY_PREDICATE", "has_model_policy_json"
        )
        errors: list[str] = []

        policy_id = self._resolve_concept_id_by_name(
            policy_name, preferred_language=preferred_language
        )
        if not policy_id:
            errors.append("policy_not_resolved")

        predicate_id = self._resolve_concept_id_by_name(
            predicate_name, preferred_language=preferred_language
        )
        if not predicate_id:
            errors.append("predicate_not_resolved")

        policy_payload: Mapping[str, Any] | None = None
        policy_source = "none"

        # Try graph-based resolution first (JVNAUTOSCI-998)
        if policy_id:
            try:
                from src.backend.services.workflow_policy_graph_service import (
                    resolve_policy_from_graph,
                )

                graph_policy = resolve_policy_from_graph(policy_id)
                if graph_policy and isinstance(graph_policy, Mapping):
                    policy_payload = graph_policy
                    policy_source = "graph"
            except Exception:
                pass  # Fall through to JSON fallback

        # JSON fallback if graph resolution failed
        if policy_payload is None and policy_id and predicate_id:
            try:
                from src.backend.services.text_value_service import (
                    get_texts_for_concept,
                )
            except Exception:
                get_texts_for_concept = None  # type: ignore[assignment]

            rows: list[dict[str, Any]] | None = None
            if callable(get_texts_for_concept):
                try:
                    rows = get_texts_for_concept(
                        policy_id, predicate=predicate_id, limit=5
                    )
                except Exception:
                    rows = None

            if not rows:
                errors.append("policy_text_missing")
            else:
                try:
                    import json
                except Exception:
                    json = None  # type: ignore[assignment]

                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    raw_text = row.get("text")
                    if not isinstance(raw_text, str) or not raw_text.strip():
                        continue
                    parsed = None
                    if json is not None:
                        try:
                            parsed = json.loads(raw_text)
                        except Exception:
                            parsed = None
                    if isinstance(parsed, Mapping):
                        policy_payload = parsed
                        policy_source = "json"
                        break
                    errors.append("policy_json_invalid")

        state = _WorkflowModelPolicyState(
            enabled=enabled,
            policy=policy_payload,
            policy_id=policy_id,
            predicate_id=predicate_id,
            errors=tuple(errors),
        )

        telemetry: dict[str, Any] = {
            "type": "workflow_model_policy",
            "enabled": enabled,
            "policy_id": policy_id or "",
            "predicate_id": predicate_id or "",
            "loaded": bool(policy_payload),
            "policy_source": policy_source,
            "errors": list(errors),
        }

        self._workflow_model_policy_cache = {
            "expires_at": now + float(self._workflow_model_policy_cache_ttl_seconds),
            "state": state,
            "telemetry": telemetry,
        }

        return state, telemetry

    def _select_model_for_stage(
        self,
        *,
        stage: str,
        default_model: Optional[str],
        policy_state: _WorkflowModelPolicyState,
        registry_snapshot: Mapping[str, Any] | None = None,
    ) -> Optional[str]:
        if not policy_state.enabled:
            return default_model

        candidates = self._stage_model_candidates(
            stage=stage,
            default_model=default_model,
            policy_state=policy_state,
            registry_snapshot=registry_snapshot,
        )

        for candidate in candidates:
            if candidate.source == "active_llm" and default_model:
                return default_model
            model_name = self._normalise_llm_model_name(candidate.model)
            if model_name:
                return model_name

        return default_model

    def _create_client_for_candidate(
        self,
        candidate: _ModelCandidate,
        *,
        default_client: Any,
        default_model: Optional[str],
        user_concept_id: Optional[str],
        org_concept_id: Optional[str],
    ) -> tuple[Any, Optional[str], Mapping[str, Any]]:
        telemetry: dict[str, Any] = {
            "raw": candidate.raw,
            "provider": candidate.provider,
            "model": candidate.model,
            "host": candidate.host,
            "source": candidate.source,
        }

        if candidate.source == "active_llm":
            resolved_model = default_model
            resolved_provider = None
            try:
                from src.backend.languagemodels.llm_interface import (
                    resolve_provider_from_model_concept,
                    resolve_openai_model_name,
                    resolve_ollama_model_name,
                )

                resolved_provider = resolve_provider_from_model_concept(default_model)
                if resolved_provider == "openai":
                    resolved_model = resolve_openai_model_name(default_model)
                elif resolved_provider == "ollama":
                    resolved_model = resolve_ollama_model_name(default_model)
            except Exception:
                resolved_provider = None

            if resolved_provider:
                telemetry["provider"] = resolved_provider
            if resolved_model:
                telemetry["model"] = resolved_model
            return default_client, resolved_model or default_model, telemetry

        provider = candidate.provider
        model = self._normalise_llm_model_name(candidate.model)

        if isinstance(candidate.raw, str) and candidate.raw.strip().startswith("#V#"):
            try:
                from src.backend.languagemodels.llm_interface import (
                    resolve_provider_from_model_concept,
                    resolve_openai_model_name,
                    resolve_ollama_model_name,
                )

                resolved_provider = resolve_provider_from_model_concept(candidate.raw)
                if resolved_provider:
                    provider = resolved_provider
                if provider == "openai":
                    model = resolve_openai_model_name(candidate.raw) or model
                elif provider == "ollama":
                    model = resolve_ollama_model_name(candidate.raw) or model
            except Exception:
                pass

        if provider in {None, "", "openai", "ollama", "gemini"}:
            pass
        else:
            telemetry["error"] = "unsupported_provider"
            return default_client, default_model, telemetry

        try:
            from src.backend.languagemodels.llm_interface import get_llm_client

            if provider:
                client = get_llm_client(
                    client_type=provider,
                    user_concept_id=user_concept_id,
                    org_concept_id=org_concept_id,
                )
            else:
                client = default_client
            return client, model or default_model, telemetry
        except Exception as exc:
            telemetry["error"] = str(exc)
            return default_client, default_model, telemetry

    def _run_llm_with_fallbacks(
        self,
        *,
        stage: str,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]],
        default_client: Any,
        default_model: Optional[str],
        policy_state: _WorkflowModelPolicyState,
        registry_snapshot: Mapping[str, Any] | None,
        user_concept_id: Optional[str],
        org_concept_id: Optional[str],
        llm_calls_log: list[dict[str, Any]],
        aux_log: list[Mapping[str, Any]],
        record_llm_call: Callable[..., Any],
    ) -> tuple[str, Optional[str], Mapping[str, Any]]:
        candidates = self._stage_model_candidates(
            stage=stage,
            default_model=default_model,
            policy_state=policy_state,
            registry_snapshot=registry_snapshot,
        )

        errors: list[Mapping[str, Any]] = []
        last_exception: Exception | None = None

        for candidate in candidates:
            client, model_name, telemetry = self._create_client_for_candidate(
                candidate,
                default_client=default_client,
                default_model=default_model,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
            )

            llm_start = time.perf_counter()
            try:
                response = client.generate(
                    prompt,
                    context=cast(Optional[List[Dict[str, Any]]], context),
                    model=model_name,
                )
                duration_ms = (time.perf_counter() - llm_start) * 1000.0
                record_llm_call(
                    call_type="llm.generate",
                    model_name=model_name,
                    duration_ms=duration_ms,
                    usage=None,
                    note=("Fallback chain generate()" if errors else "llm.generate"),
                    stage=stage,
                    provider=(
                        telemetry.get("provider")
                        if isinstance(telemetry, Mapping)
                        else None
                    ),
                    candidate=telemetry,
                )
                aux_log.append(
                    {
                        "type": "workflow_model_policy_stage",
                        "stage": stage,
                        "selected": {
                            **telemetry,
                            "model_resolved": model_name,
                        },
                        "fallback_used": bool(errors),
                        "errors": list(errors),
                    }
                )
                return response, model_name, telemetry
            except Exception as exc:
                duration_ms = (time.perf_counter() - llm_start) * 1000.0
                record_llm_call(
                    call_type="llm.generate",
                    model_name=model_name,
                    duration_ms=duration_ms,
                    usage=None,
                    note="llm.generate failed; trying fallback",
                    stage=stage,
                    provider=(
                        telemetry.get("provider")
                        if isinstance(telemetry, Mapping)
                        else None
                    ),
                    candidate=telemetry,
                )
                error_entry = {
                    "candidate": telemetry,
                    "model_resolved": model_name,
                    "error": str(exc),
                }
                errors.append(error_entry)
                last_exception = exc
                continue

        if errors:
            aux_log.append(
                {
                    "type": "workflow_model_policy_stage",
                    "stage": stage,
                    "selected": None,
                    "fallback_used": True,
                    "errors": list(errors),
                }
            )

        if last_exception:
            raise last_exception
        raise RuntimeError("No model candidates available for stage")

    def _run_llm_with_tools_fallbacks(
        self,
        *,
        stage: str,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]],
        tool_definitions: Sequence[ToolDefinition],
        default_client: Any,
        default_model: Optional[str],
        policy_state: _WorkflowModelPolicyState,
        registry_snapshot: Mapping[str, Any] | None,
        user_concept_id: Optional[str],
        org_concept_id: Optional[str],
        llm_calls_log: list[dict[str, Any]],
        aux_log: list[Mapping[str, Any]],
        record_llm_call: Callable[..., Any],
    ) -> tuple[LLMResponse, Optional[str], Mapping[str, Any]]:
        candidates = self._stage_model_candidates(
            stage=stage,
            default_model=default_model,
            policy_state=policy_state,
            registry_snapshot=registry_snapshot,
        )

        errors: list[Mapping[str, Any]] = []
        last_exception: Exception | None = None

        for candidate in candidates:
            client, model_name, telemetry = self._create_client_for_candidate(
                candidate,
                default_client=default_client,
                default_model=default_model,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
            )

            supports_structured = (
                hasattr(client, "generate_with_tools")
                and hasattr(client, "_should_use_structured_calling")
                and client._should_use_structured_calling()
            )
            if not supports_structured:
                errors.append(
                    {
                        "candidate": telemetry,
                        "model_resolved": model_name,
                        "error": "structured_tool_calling_disabled",
                    }
                )
                continue

            llm_start = time.perf_counter()
            try:
                llm_response = client.generate_with_tools(
                    prompt=prompt,
                    available_tools=list(tool_definitions),
                    context=cast(Optional[List[Dict[str, Any]]], context),
                    model=model_name,
                    system_message=None,
                )
                duration_ms = (time.perf_counter() - llm_start) * 1000.0
                record_llm_call(
                    call_type="llm.generate_with_tools",
                    model_name=(
                        llm_response.model
                        if isinstance(getattr(llm_response, "model", None), str)
                        else model_name
                    ),
                    duration_ms=duration_ms,
                    usage=(
                        llm_response.usage
                        if isinstance(getattr(llm_response, "usage", None), Mapping)
                        else None
                    ),
                    stage=stage,
                    provider=(
                        telemetry.get("provider")
                        if isinstance(telemetry, Mapping)
                        else None
                    ),
                    candidate=telemetry,
                )
                aux_log.append(
                    {
                        "type": "workflow_model_policy_stage",
                        "stage": stage,
                        "selected": {
                            **telemetry,
                            "model_resolved": model_name,
                        },
                        "fallback_used": bool(errors),
                        "errors": list(errors),
                    }
                )
                return llm_response, model_name, telemetry
            except Exception as exc:
                duration_ms = (time.perf_counter() - llm_start) * 1000.0
                record_llm_call(
                    call_type="llm.generate_with_tools",
                    model_name=model_name,
                    duration_ms=duration_ms,
                    usage=None,
                    note="llm.generate_with_tools failed; trying fallback",
                    stage=stage,
                    provider=(
                        telemetry.get("provider")
                        if isinstance(telemetry, Mapping)
                        else None
                    ),
                    candidate=telemetry,
                )
                errors.append(
                    {
                        "candidate": telemetry,
                        "model_resolved": model_name,
                        "error": str(exc),
                    }
                )
                last_exception = exc
                continue

        if errors:
            aux_log.append(
                {
                    "type": "workflow_model_policy_stage",
                    "stage": stage,
                    "selected": None,
                    "fallback_used": True,
                    "errors": list(errors),
                }
            )

        if last_exception:
            raise last_exception
        raise RuntimeError("No structured model candidates available for stage")

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

        # Look for ```json or ``` fences.
        # NOTE: Some models emit an opening fence but omit the closing fence.
        # We attempt a safe recovery for that failure mode by treating the rest
        # of the response as the fenced content.
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
                fenced_content = response[content_start:].strip()
            else:
                fenced_content = response[content_start:end].strip()

            if not fenced_content:
                continue

            if not (fenced_content.startswith("{") or fenced_content.startswith("[")):
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
        - Concatenated JSON objects are rejected unless they are separated by
          whitespace and each value is a valid tool-call payload (these are
          merged into a single batch).
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
                        else:
                            # Unterminated fence recovery: treat the remainder of the
                            # response as the fenced JSON payload.
                            raw = fenced[open_line_end + 1 :].strip()

        looks_like_tool_call = any(
            token in raw
            for token in (
                f'"{self._ACTION_FIELD}"',
                f'"{self._TOOL_FIELD}"',
                f'"{self._PAYLOAD_FIELD}"',
                '"call_tool"',
                '"tool_calls"',
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
            repaired = self._repair_truncated_json(raw)
            if repaired is not None:
                try:
                    parsed, end = decoder.raw_decode(repaired)
                    raw = repaired
                    self._logger.warning(
                        "[mcp_orchestrator] Repaired truncated tool-call JSON payload."
                    )
                except json.JSONDecodeError as repair_exc:
                    raise ToolCallParsingError(
                        "Tool call was not executed: invalid JSON in tool call response.",
                        raw_response=text,
                    ) from repair_exc
            else:
                raise ToolCallParsingError(
                    "Tool call was not executed: invalid JSON in tool call response.",
                    raw_response=text,
                ) from exc

        raw_trailing = raw[end:]
        trailing = raw_trailing.strip()

        def _normalise_tool_call(candidate: Any) -> _ToolCallRequest | None:
            if not isinstance(candidate, MutableMapping):
                return None

            tool_name = candidate.get(self._TOOL_FIELD)
            if not isinstance(tool_name, str):
                alt_name = candidate.get("name")
                if isinstance(alt_name, str) and alt_name:
                    tool_name = alt_name
            payload_value = candidate.get(self._PAYLOAD_FIELD)
            action_value = candidate.get(self._ACTION_FIELD)
            if not isinstance(payload_value, MutableMapping):
                for alt_key in ("params", "arguments", "args"):
                    alt_value = candidate.get(alt_key)
                    if isinstance(alt_value, str) and alt_value.strip():
                        try:
                            parsed_alt = json.loads(alt_value)
                        except json.JSONDecodeError:
                            parsed_alt = None
                        if isinstance(parsed_alt, MutableMapping):
                            payload_value = parsed_alt
                            break
                    if isinstance(alt_value, MutableMapping):
                        payload_value = alt_value
                        break

            has_tool = isinstance(tool_name, str)
            has_payload = isinstance(payload_value, MutableMapping)
            has_action = action_value == self._CALL_ACTION

            missing_action = self._ACTION_FIELD not in candidate
            strict_shape = set(candidate.keys()) <= {
                self._TOOL_FIELD,
                self._PAYLOAD_FIELD,
                "params",
                "arguments",
                "args",
                "name",
                "id",
                "tool_call_id",
            }

            if missing_action and has_tool and has_payload and strict_shape:
                catalogue = None
                describe_methods = getattr(self._gateway, "describe_methods", None)
                if callable(describe_methods):
                    catalogue = describe_methods()
                if isinstance(catalogue, Mapping) and tool_name in catalogue:
                    has_action = True
                else:
                    has_action = True

            if not (has_action and has_tool and has_payload):
                return None

            tool_call: _ToolCallRequest = {
                self._ACTION_FIELD: self._CALL_ACTION,
                self._TOOL_FIELD: tool_name,
                self._PAYLOAD_FIELD: cast(MutableMapping[str, Any], payload_value),
            }

            call_id = (
                candidate.get("_call_id")
                or candidate.get("id")
                or candidate.get("tool_call_id")
            )
            if isinstance(call_id, str) and call_id:
                tool_call["_call_id"] = call_id

            return tool_call

        tool_calls: list[_ToolCallRequest] = []
        parsed_list: list[Any] | None = None
        if isinstance(parsed, MutableMapping):
            wrapped_calls = parsed.get("tool_calls")
            if isinstance(wrapped_calls, list):
                parsed_list = wrapped_calls
            else:
                tool_call = _normalise_tool_call(parsed)
                if tool_call is None:
                    return None
                tool_calls = [tool_call]
        elif isinstance(parsed, list):
            parsed_list = parsed

        if parsed_list is not None:
            for item in parsed_list:
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
        elif not tool_calls:
            if looks_like_tool_call:
                raise ToolCallParsingError(
                    "Tool call was not executed: tool call must be a JSON object or array.",
                    raw_response=text,
                )
            return None

        # Reject concatenated JSON values, but tolerate non-JSON markers like
        # "[json]" that some models append after a valid tool call.
        if trailing:
            allow_multi = not (raw_trailing and raw_trailing[0] in ("{", "["))
            if allow_multi and (trailing.startswith("{") or trailing.startswith("[")):
                extra_calls: list[_ToolCallRequest] = []
                remaining = trailing
                while remaining and (
                    remaining.startswith("{") or remaining.startswith("[")
                ):
                    try:
                        extra_parsed, extra_end = decoder.raw_decode(remaining)
                    except json.JSONDecodeError:
                        extra_calls = []
                        break

                    if isinstance(extra_parsed, MutableMapping):
                        extra_call = _normalise_tool_call(extra_parsed)
                        if extra_call is None:
                            raise ToolCallParsingError(
                                "Tool call was not executed: multiple JSON values were emitted in one response.",
                                raw_response=text,
                            )
                        extra_calls.append(extra_call)
                    elif isinstance(extra_parsed, list):
                        for item in extra_parsed:
                            extra_call = _normalise_tool_call(item)
                            if extra_call is None:
                                raise ToolCallParsingError(
                                    "Tool call was not executed: multiple JSON values were emitted in one response.",
                                    raw_response=text,
                                )
                            extra_calls.append(extra_call)
                    else:
                        raise ToolCallParsingError(
                            "Tool call was not executed: multiple JSON values were emitted in one response.",
                            raw_response=text,
                        )

                    remaining = remaining[extra_end:].strip()

                if extra_calls:
                    tool_calls.extend(extra_calls)
                    trailing = remaining

            if trailing.startswith("{") or trailing.startswith("["):
                try:
                    # Only treat the remainder as a second value if it is itself
                    # valid JSON.
                    decoder.raw_decode(trailing)
                except json.JSONDecodeError:
                    pass
                else:
                    raise ToolCallParsingError(
                        "Tool call was not executed: multiple JSON values were emitted in one response.",
                        raw_response=text,
                    )

            if trailing:
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
                extra_calls: list[_ToolCallRequest] = []
                remaining = post_fence_trailing
                while remaining and (
                    remaining.startswith("{") or remaining.startswith("[")
                ):
                    try:
                        extra_parsed, extra_end = decoder.raw_decode(remaining)
                    except json.JSONDecodeError:
                        extra_calls = []
                        break

                    if isinstance(extra_parsed, MutableMapping):
                        extra_call = _normalise_tool_call(extra_parsed)
                        if extra_call is None:
                            raise ToolCallParsingError(
                                "Tool call was not executed: multiple JSON values were emitted in one response.",
                                raw_response=text,
                            )
                        extra_calls.append(extra_call)
                    elif isinstance(extra_parsed, list):
                        for item in extra_parsed:
                            extra_call = _normalise_tool_call(item)
                            if extra_call is None:
                                raise ToolCallParsingError(
                                    "Tool call was not executed: multiple JSON values were emitted in one response.",
                                    raw_response=text,
                                )
                            extra_calls.append(extra_call)
                    else:
                        raise ToolCallParsingError(
                            "Tool call was not executed: multiple JSON values were emitted in one response.",
                            raw_response=text,
                        )

                    remaining = remaining[extra_end:].strip()

                if extra_calls:
                    tool_calls.extend(extra_calls)
                    post_fence_trailing = remaining

                if post_fence_trailing.startswith(
                    "{"
                ) or post_fence_trailing.startswith("["):
                    try:
                        decoder.raw_decode(post_fence_trailing)
                    except json.JSONDecodeError:
                        pass
                    else:
                        raise ToolCallParsingError(
                            "Tool call was not executed: multiple JSON values were emitted in one response.",
                            raw_response=text,
                        )

            if post_fence_trailing:
                snippet = post_fence_trailing
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                self._logger.warning(
                    "[mcp_orchestrator] Stripping trailing non-JSON text after fenced tool call (len=%d): %r",
                    len(post_fence_trailing),
                    snippet,
                )

        return tool_calls

    def _tool_schema_for_name(
        self,
        tool_name: str,
        method_catalogue: Mapping[str, Any],
    ) -> McpSchema | None:
        get_definition = getattr(self._gateway, "get_method_definition", None)
        if callable(get_definition):
            definition = get_definition(tool_name)
            if isinstance(definition, MethodDefinition) and isinstance(
                definition.input_schema, McpSchema
            ):
                return definition.input_schema

        metadata = method_catalogue.get(tool_name)
        if not isinstance(metadata, Mapping):
            return None

        raw_schema = metadata.get("input_schema")
        if isinstance(raw_schema, McpSchema):
            return raw_schema
        if not isinstance(raw_schema, Mapping):
            return None

        def _coerce_fields(value: Any) -> Dict[str, Any]:
            if isinstance(value, Mapping):
                return dict(value)
            if isinstance(value, list):
                return {
                    item: object for item in value if isinstance(item, str) and item
                }
            return {}

        return McpSchema(
            required=_coerce_fields(raw_schema.get("required") or {}),
            optional=_coerce_fields(raw_schema.get("optional") or {}),
            allow_unknown=bool(raw_schema.get("allow_unknown")),
            description=(
                raw_schema.get("description")
                if isinstance(raw_schema.get("description"), str)
                else None
            ),
        )

    def _preflight_tool_calls(
        self,
        tool_calls: list[_ToolCallRequest],
        method_catalogue: Mapping[str, Any],
        *,
        user_namespace: str | None,
        selected_gmail_profile: str | None,
    ) -> _ToolCallPreflightResult:
        errors: list[str] = []
        warnings: list[str] = []
        tool_unavailable: list[str] = []

        if not tool_calls:
            return _ToolCallPreflightResult(None, errors, warnings, tool_unavailable)

        available_tools = set(method_catalogue.keys())
        enforce_availability = bool(available_tools)

        for tool_call in tool_calls:
            tool_name = tool_call.get(self._TOOL_FIELD)
            payload = tool_call.get(self._PAYLOAD_FIELD)

            if not isinstance(tool_name, str):
                errors.append("Tool name must be a string.")
                continue

            if enforce_availability and tool_name not in available_tools:
                tool_unavailable.append(tool_name)
                errors.append(f"Tool '{tool_name}' is not available.")
                continue

            if not isinstance(payload, MutableMapping):
                errors.append(f"Tool '{tool_name}' payload must be a JSON object.")
                continue

            schema = self._tool_schema_for_name(tool_name, method_catalogue)

            self._apply_payload_defaults(
                tool_name,
                payload,
                schema=schema,
                user_namespace=user_namespace,
                selected_gmail_profile=selected_gmail_profile,
            )
            if schema is None:
                continue

            _, coercion_warnings = coerce_payload_types(schema, payload)
            warnings.extend(
                [f"{tool_name}: {warning}" for warning in coercion_warnings]
            )

            validation_payload = payload
            if (
                not schema.allow_unknown
                and "namespace" in payload
                and "namespace" not in schema.required
                and "namespace" not in schema.optional
            ):
                validation_payload = dict(payload)
                validation_payload.pop("namespace", None)

            ok, validation_errors = validate_payload(schema, validation_payload)
            if not ok:
                errors.extend([f"{tool_name}: {error}" for error in validation_errors])

        return _ToolCallPreflightResult(tool_calls, errors, warnings, tool_unavailable)

    def _apply_payload_defaults(
        self,
        tool_name: str,
        payload: MutableMapping[str, Any],
        *,
        schema: McpSchema | None,
        user_namespace: str | None,
        selected_gmail_profile: str | None,
    ) -> None:
        if tool_name.startswith("gmail_"):
            if not payload.get("profile") and selected_gmail_profile:
                payload["profile"] = selected_gmail_profile
            if "namespace" in payload:
                payload.pop("namespace", None)
            return

        if user_namespace and "namespace" not in payload:
            payload["namespace"] = user_namespace

    def _tool_call_repair_enabled(self) -> bool:
        return os.getenv("VON_TOOL_CALL_REPAIR_ENABLE", "1").lower() in {
            "1",
            "true",
        }

    def _attempt_tool_call_repair(
        self,
        *,
        current_response: str,
        errors: Sequence[str],
        tool_list: Sequence[str],
        llm_client: Any,
        policy_state: _WorkflowModelPolicyState,
        default_model: str | None,
        registry_snapshot: Mapping[str, Any] | None,
        user_concept_id: str | None,
        org_concept_id: str | None,
        aux_llm_calls: list[Mapping[str, Any]],
        llm_calls_log: list[dict[str, Any]],
        record_llm_call: Callable[..., None] | None,
    ) -> list[_ToolCallRequest] | None:
        if not self._tool_call_repair_enabled():
            return None

        variables = {
            "tool_list": "\n".join(f"- {name}" for name in tool_list),
            "errors": "\n".join(f"- {err}" for err in errors),
            "raw_tool_call": (current_response[:4000] if current_response else ""),
        }
        rendered_prompt = self._prompt_templates.render_prompt(
            self._TOOL_CALL_REPAIR_PROMPTS,
            variables=variables,
            fallback=self._TOOL_CALL_REPAIR_PROMPT.format(**variables),
            max_chars=4000,
        )
        prompt_text = (
            rendered_prompt.text
            if rendered_prompt is not None
            else self._TOOL_CALL_REPAIR_PROMPT.format(**variables)
        )

        try:
            aux_llm_calls.append(
                {
                    "type": "tool_call_repair",
                    "stage": "prompt",
                    "prompt_preview": prompt_text[:800],
                }
            )
        except Exception:
            pass

        repaired_response = None
        if isinstance(policy_state, _WorkflowModelPolicyState) and callable(
            record_llm_call
        ):
            repaired_response, _, _ = self._run_llm_with_fallbacks(
                stage="tool_recovery",
                prompt=prompt_text,
                context=[],
                default_client=llm_client,
                default_model=default_model,
                policy_state=policy_state,
                registry_snapshot=registry_snapshot,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
                llm_calls_log=llm_calls_log,
                aux_log=aux_llm_calls,
                record_llm_call=record_llm_call,
            )
        else:
            repaired_response = llm_client.generate(
                prompt_text, context=[], model=default_model
            )

        try:
            aux_llm_calls.append(
                {
                    "type": "tool_call_repair",
                    "stage": "response",
                    "response_preview": (
                        repaired_response[:800]
                        if isinstance(repaired_response, str)
                        else str(repaired_response)[:800]
                    ),
                }
            )
        except Exception:
            pass

        try:
            return self._extract_tool_calls(
                repaired_response
                if isinstance(repaired_response, str)
                else str(repaired_response)
            )
        except ToolCallParsingError:
            return None

    @staticmethod
    def _repair_truncated_json(raw: str) -> str | None:
        """Attempt to close unterminated JSON when it looks safely truncated."""
        if not raw:
            return None

        stack: list[str] = []
        in_string = False
        escaped = False
        pairs = {"{": "}", "[": "]"}

        for ch in raw:
            if in_string:
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                    continue
                if ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue
            if ch in pairs:
                stack.append(ch)
                continue
            if ch in ("}", "]"):
                if not stack:
                    return None
                opening = stack.pop()
                if pairs.get(opening) != ch:
                    return None

        if in_string or not stack:
            return None

        closing = "".join(pairs[ch] for ch in reversed(stack))
        candidate = raw + closing
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return candidate

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
                        else:
                            # Unterminated fence recovery: treat the remainder of the
                            # response as the fenced JSON payload.
                            raw = fenced[open_line_end + 1 :].strip()

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
            repaired = self._repair_truncated_json(raw)
            if repaired is not None:
                try:
                    parsed, end = decoder.raw_decode(repaired)
                    raw = repaired
                    self._logger.warning(
                        "[mcp_orchestrator] Repaired truncated tool-call JSON payload."
                    )
                except json.JSONDecodeError as repair_exc:
                    raise ToolCallParsingError(
                        "Tool call was not executed: invalid JSON in tool call response.",
                        raw_response=text,
                    ) from repair_exc
            else:
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
                try:
                    decoder.raw_decode(trailing)
                except json.JSONDecodeError:
                    pass
                else:
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
                try:
                    decoder.raw_decode(post_fence_trailing)
                except json.JSONDecodeError:
                    pass
                else:
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

    @staticmethod
    def _normalise_language(language: str | None) -> str | None:
        if not isinstance(language, str):
            return None
        cleaned = language.strip()
        return cleaned if cleaned else None

    @staticmethod
    def _language_variants(language: str | None) -> list[str]:
        if not isinstance(language, str) or not language.strip():
            return []
        cleaned = language.strip()
        base = cleaned.split("-")[0] if "-" in cleaned else cleaned
        if base and base != cleaned:
            return [cleaned, base]
        return [cleaned]

    @staticmethod
    def _select_preferred_name(
        name_texts: list[dict[str, Any]], preferred_language: str | None
    ) -> tuple[str | None, str | None]:
        if not name_texts:
            return None, None

        variants = InternalMCPChatOrchestrator._language_variants(preferred_language)

        def _type_rank(name_type: str | None) -> int:
            if name_type == "NL":
                return 0
            if name_type == "ABBR":
                return 1
            if name_type == "CODE":
                return 2
            return 3

        candidates: list[tuple[int, int, str, str | None]] = []
        for entry in name_texts:
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            lang = entry.get("lang")
            context = entry.get("context")
            name_type = None
            if isinstance(context, dict):
                name_type = context.get("name_type")
            if not isinstance(name_type, str):
                name_type = None

            lang_rank = 2
            if variants and isinstance(lang, str):
                if lang == variants[0]:
                    lang_rank = 0
                elif len(variants) > 1 and lang == variants[1]:
                    lang_rank = 1

            candidates.append(
                (
                    lang_rank,
                    _type_rank(name_type),
                    text.strip(),
                    lang if isinstance(lang, str) else None,
                )
            )

        if not candidates:
            return None, None

        candidates.sort(key=lambda item: (item[0], item[1], len(item[2]), item[2]))
        best = candidates[0]
        return best[2], best[3]

    def _get_preflight_cache_key(self, preferred_language: str | None) -> str:
        return (preferred_language or "").strip() or "__default__"

    def _load_preflight_predicates(
        self, preferred_language: str | None
    ) -> list[dict[str, Any]]:
        try:
            from src.backend.services.concept_search_service import search_concepts
        except Exception:
            return []

        try:
            from src.backend.services.text_value_service import get_texts_for_concept
        except Exception:
            return []

        try:
            result = search_concepts(
                query="",
                instance_of=self._PREFLIGHT_PREDICATE_TYPE_ID,
                filter_kind=["predicate"],
                limit=80,
                match_type="substring",
            )
        except Exception:
            return []

        results = result.get("results") if isinstance(result, dict) else None
        if not isinstance(results, list):
            return []

        predicates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in results:
            if not isinstance(entry, dict):
                continue
            concept_id = entry.get("concept_id")
            if not isinstance(concept_id, str) or not concept_id:
                continue
            if concept_id in seen:
                continue
            name_texts = get_texts_for_concept(
                concept_id, predicate="hasName", limit=80
            )
            display_name, name_lang = self._select_preferred_name(
                name_texts, preferred_language
            )
            display_name = self._normalise_preflight_display_name(display_name)
            predicates.append(
                {
                    "concept_id": concept_id,
                    "display_name": display_name,
                    "name_lang": name_lang,
                }
            )
            seen.add(concept_id)

        return predicates

    def _build_ontology_preflight(
        self, prompt: str, preferred_language: str | None
    ) -> _OntologyPreflightResult:
        """Deterministically surface preflight predicates from the Vontology.

        Stage 1 (JVNAUTOSCI-988): read-only, cheap, and scoped to the current turn.
        """

        if not isinstance(prompt, str):
            return _OntologyPreflightResult(message=None, telemetry=None)

        raw = prompt.strip()
        if not raw:
            return _OntologyPreflightResult(message=None, telemetry=None)

        explicit_ids = sorted(set(re.findall(r"#V#[A-Za-z0-9][A-Za-z0-9._-]*", raw)))

        preferred_language = self._normalise_language(preferred_language)
        if preferred_language is None:
            try:
                from src.backend.services.settings_service import get_preferred_language

                preferred_language = self._normalise_language(get_preferred_language())
            except Exception:
                preferred_language = None

        cache_key = self._get_preflight_cache_key(preferred_language)
        cached = self._preflight_cache.get(cache_key)
        now = time.time()
        if (
            cached
            and (now - cached.get("timestamp", 0)) < self._PREFLIGHT_CACHE_TTL_SECONDS
        ):
            predicates = cached.get("predicates", [])
        else:
            predicates = self._load_preflight_predicates(preferred_language)
            self._preflight_cache[cache_key] = {
                "timestamp": now,
                "predicates": predicates,
            }

        targeted_predicates: list[dict[str, Any]] = []
        predicate_query: str | None = None

        if self._should_trigger_predicate_search(raw):
            predicate_query = self._extract_predicate_query_from_text(raw)
            if predicate_query:
                try:
                    from src.backend.services.concept_search_service import (
                        search_concepts,
                    )
                except Exception:
                    search_concepts = None

                if callable(search_concepts):
                    try:
                        result = search_concepts(
                            query=predicate_query,
                            filter_kind=["predicate"],
                            match_type="similarity",
                            min_similarity=0.55,
                            limit=12,
                        )
                    except Exception:
                        result = None

                    entries = (
                        result.get("results") if isinstance(result, dict) else None
                    )
                    if isinstance(entries, list):
                        seen_targeted: set[str] = set()
                        for entry in entries:
                            if not isinstance(entry, dict):
                                continue
                            concept_id = entry.get("concept_id")
                            if not isinstance(concept_id, str) or not concept_id:
                                continue
                            if concept_id in seen_targeted:
                                continue
                            name_value = entry.get("name")
                            if isinstance(name_value, str) and name_value.strip():
                                name_value = self._normalise_preflight_display_name(
                                    name_value
                                )
                            targeted_predicates.append(
                                {
                                    "concept_id": concept_id,
                                    "display_name": name_value,
                                }
                            )
                            seen_targeted.add(concept_id)

        if not predicates and not explicit_ids and not targeted_predicates:
            return _OntologyPreflightResult(message=None, telemetry=None)

        lines: list[str] = [
            "ONTOLOGY PRE-FLIGHT (deterministic, read-only; stage=1):",
            "Source: instances of #V#conversation_preflight_predicate.",
            "Use these existing predicate concept IDs for tool planning. Do not invent new predicates here.",
        ]

        if explicit_ids:
            lines.append("Explicit IDs in prompt:")
            for concept_id in explicit_ids[:6]:
                lines.append(f"- {concept_id}")

        if predicates:
            lines.append("Preflight predicate candidates:")
            for item in predicates[:30]:
                concept_id = item.get("concept_id")
                display_name = item.get("display_name")
                name_lang = item.get("name_lang")
                if not concept_id:
                    continue
                if display_name:
                    lang_suffix = f" [{name_lang}]" if name_lang else ""
                    lines.append(f'- {concept_id} (name="{display_name}"{lang_suffix})')
                else:
                    lines.append(f"- {concept_id}")

        if targeted_predicates:
            lines.append("Targeted predicate candidates (from prompt):")
            for item in targeted_predicates[:12]:
                concept_id = item.get("concept_id")
                display_name = item.get("display_name")
                if not concept_id:
                    continue
                if display_name:
                    lines.append(f'- {concept_id} (name="{display_name}")')
                else:
                    lines.append(f"- {concept_id}")

        telemetry: dict[str, Any] = {
            "type": "ontology_preflight",
            "stage": "deterministic_preflight",
            "preferred_language": preferred_language,
            "predicate_type": self._PREFLIGHT_PREDICATE_TYPE_ID,
            "preflight_predicates": predicates,
            "explicit_ids": explicit_ids,
            "predicate_query": predicate_query,
            "targeted_predicates": targeted_predicates,
        }

        return _OntologyPreflightResult(message="\n".join(lines), telemetry=telemetry)

    def _build_augmented_context(
        self,
        context: Optional[Sequence[Mapping[str, Any]]],
        user_namespace: str | None = None,
        auxiliary_system_prompt: str | None = None,
        preflight_message: str | None = None,
        preferred_language: str | None = None,
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
            preferred_language=preferred_language,
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
        if isinstance(preflight_message, str) and preflight_message.strip():
            base.insert(1, {"role": "system", "content": preflight_message.strip()})
        return self._limit_context_for_llm(base)

    @staticmethod
    def _extract_recent_user_prompts(
        context: Optional[Sequence[Mapping[str, Any]]],
        *,
        max_count: int = 5,
    ) -> list[str]:
        if not context:
            return []
        prompts: list[str] = []
        for msg in context:
            if not isinstance(msg, Mapping):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            cleaned = content.strip()
            if cleaned:
                prompts.append(cleaned)
        if max_count > 0 and len(prompts) > max_count:
            return prompts[-max_count:]
        return prompts

    @staticmethod
    def _prompt_requests_tts_voice(prompt: str) -> bool:
        if not isinstance(prompt, str):
            return False
        text = prompt.strip().lower()
        if not text:
            return False

        # Require an explicit voice/speech hint to avoid spurious matches.
        if not any(
            token in text for token in ("voice", "tts", "text to speech", "speech")
        ):
            return False

        # Typical phrasings users use when asking what voice is active.
        patterns = (
            r"\bwhat\s+voice\b",
            r"\bwhich\s+voice\b",
            r"\bcurrent\s+voice\b",
            r"\btts\s+voice\b",
            r"\bvoice\s+are\s+you\s+using\b",
            r"\bvoice\s+am\s+i\s+using\b",
            r"\bwhat\s+tts\b",
        )

        return any(re.search(p, text) for p in patterns)

    def _build_client_capabilities_voice_hint(self) -> str | None:
        """Return a small, safe system hint about the current TTS voice.

        Uses the per-session client capability snapshot (client-reported and
        non-authoritative).
        """

        try:
            from flask import has_request_context

            if not has_request_context():
                return None
        except Exception:
            return None

        try:
            from ...services.client_capabilities_service import (
                get_client_capabilities_snapshot,
            )

            snapshot = get_client_capabilities_snapshot()
        except Exception:
            snapshot = None

        speech = (
            snapshot.get("speech_synthesis") if isinstance(snapshot, dict) else None
        )
        speech = speech if isinstance(speech, dict) else {}
        raw_settings = speech.get("settings")
        settings = raw_settings if isinstance(raw_settings, dict) else {}

        voice_name = settings.get("voice_name")
        default_voice_lang = speech.get("default_voice_lang")
        supported = speech.get("supported")
        voices_count = speech.get("voices_count")

        def _clean(value: Any, *, max_chars: int = 140) -> str | None:
            if value is None:
                return None
            text = str(value).strip()
            if not text:
                return None
            return text[:max_chars]

        voice_name = _clean(voice_name)
        default_voice_lang = _clean(default_voice_lang, max_chars=40)

        # Keep this as a short, deterministic summary so it doesn't bloat context.
        return (
            "Client-reported speech synthesis settings (non-authoritative): "
            f"supported={supported!r}, voices_count={voices_count!r}, "
            f"default_voice_lang={default_voice_lang!r}, voice_name={voice_name!r}. "
            "If voice_name is None, the browser has not reported a selected voice yet."
        )

    def _build_client_capabilities_narration_timing_hint(self) -> str | None:
        """Return a small, safe timing hint for narration generation.

        Uses the per-session client capability snapshot (client-reported and
        non-authoritative).
        """

        try:
            from flask import has_request_context

            if not has_request_context():
                return None
        except Exception:
            return None

        try:
            from ...services.client_capabilities_service import (
                get_client_capabilities_snapshot,
            )

            snapshot = get_client_capabilities_snapshot()
        except Exception:
            snapshot = None

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

        if preferred_int is None and maximum_int is None:
            return None

        if preferred_int is not None:
            preferred_int = max(1, min(preferred_int, 600))
        if maximum_int is not None:
            maximum_int = max(1, min(maximum_int, 600))

        effective_preferred = preferred_int
        if preferred_int is not None and maximum_int is not None:
            effective_preferred = min(preferred_int, maximum_int)

        # Keep this deterministic and short to avoid context bloat.
        return (
            "Speech timing hint (client-reported, non-authoritative): "
            f"preferred_speaking_seconds={effective_preferred!r}, "
            f"max_speaking_seconds={maximum_int!r}. "
            "Aim for about preferred_speaking_seconds seconds and do not exceed max_speaking_seconds."
        )

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
        classifier_model: str | None,
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
                    fallback_model=classifier_model or model,
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
            "NOW respond with ONLY a tool-call JSON object or a JSON array of tool-call objects. "
            "Choose a tool that matches the user's request; do NOT call write tools unless the user explicitly asked for the write-side effect (e.g., Vontology changes or downloading/storing an artefact). "
            "If the user provided an arXiv ID/URL and asked to download/store/upload/finalise an artefact, call download_paper or finalise_cached_paper with the arxiv_id (do NOT call list_papers). "
            "No prose. No Markdown. Do NOT wrap the JSON in ``` fences (including ```json). "
            "The first character MUST be an opening curly brace or an opening square bracket, and the response must contain only valid JSON."
        )

    @staticmethod
    def _extract_arxiv_id_from_text(text: str) -> str | None:
        if not isinstance(text, str):
            return None

        import re

        raw = text.strip()
        if not raw:
            return None

        lowered = raw.lower()

        url_match = re.search(
            r"arxiv\.org/(?:abs|pdf)/(?P<id>(?:[a-z\-]+/\d{7})|(?:\d{4}\.\d{4,5}))(?:v\d+)?",
            lowered,
        )
        if url_match:
            return url_match.group("id")

        prefix_match = re.search(
            r"\barxiv:\s*(?P<id>(?:[a-z\-]+/\d{7})|(?:\d{4}\.\d{4,5}))(?:v\d+)?\b",
            lowered,
        )
        if prefix_match:
            return prefix_match.group("id")

        bare_match = re.search(
            r"\b(?P<id>(?:[a-z\-]+/\d{7})|(?:\d{4}\.\d{4,5}))(?:v\d+)?\b",
            lowered,
        )
        if bare_match:
            return bare_match.group("id")

        return None

    @staticmethod
    def _normalise_preflight_display_name(value: str | None) -> str | None:
        """Normalise display names by stripping control characters.

        NOTE: Previously used unicode_escape which converted backslash sequences
        like \\f into control characters (form feed). This caused corrupted names
        like "member O\f" in telemetry. Now we simply strip control chars instead.
        """
        if not isinstance(value, str):
            return value
        # Strip ASCII control characters (0x00-0x1F, 0x7F) that can corrupt display
        import re

        return re.sub(r"[\x00-\x1f\x7f]", "", value)

    @staticmethod
    def _extract_predicate_query_from_text(text: str) -> str | None:
        if not isinstance(text, str):
            return None
        raw = text.strip()
        if not raw:
            return None

        import re

        quoted = re.findall(r'"([^"]{3,80})"|\'([^\']{3,80})\'', raw)
        for pair in quoted:
            candidate = next((item for item in pair if item), None)
            if candidate:
                return candidate.strip()

        keyword_match = re.search(
            r"\b(predicate|relationship|relation|related to)\b\s*(?:called|named)?\s*([A-Za-z0-9_\-\s]{3,60})",
            raw,
            re.IGNORECASE,
        )
        if keyword_match:
            return keyword_match.group(2).strip()

        words = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", raw)
        if not words:
            return None
        return " ".join(words[:6]).strip()

    @staticmethod
    def _should_trigger_predicate_search(text: str) -> bool:
        if not isinstance(text, str) or not text.strip():
            return False
        import re

        lowered = text.lower()
        if re.search(r"#V#[A-Za-z0-9][A-Za-z0-9._-]*", text):
            return False
        return any(
            token in lowered
            for token in (
                "predicate",
                "relationship",
                "relation",
                "related to",
                "is an instance of",
                "is a type of",
                "has author",
            )
        )

    def _infer_missing_tool_call_retry_tool_calls(
        self,
        augmented_context: Sequence[Mapping[str, Any]],
        user_prompt: Any | None = None,
    ) -> list[_ToolCallRequest] | None:
        """Best-effort deterministic recovery for common missing-tool-call cases.

        This is intentionally narrow: we only force a write tool when the user
        explicitly requests the write-side effect.
        """

        last_user_text: str | None = None
        if isinstance(user_prompt, str) and user_prompt.strip():
            last_user_text = user_prompt.strip()

        if last_user_text is None:
            try:
                for item in reversed(list(augmented_context or [])):
                    if not isinstance(item, Mapping):
                        continue
                    if str(item.get("role") or "") != "user":
                        continue
                    content = item.get("content")
                    if isinstance(content, str) and content.strip():
                        last_user_text = content
                        break
            except Exception:
                last_user_text = None

        if not last_user_text:
            return None

        if self._should_trigger_predicate_search(last_user_text):
            predicate_query = self._extract_predicate_query_from_text(last_user_text)
            if predicate_query:
                return [
                    {
                        "action": "call_tool",
                        "tool": "search_concepts",
                        "payload": {
                            "query": predicate_query,
                            "filter_kind": ["predicate"],
                            "match_type": "similarity",
                            "min_similarity": 0.55,
                            "limit": 12,
                        },
                    }
                ]

        arxiv_id = self._extract_arxiv_id_from_text(last_user_text)
        if not arxiv_id:
            return None

        lowered = last_user_text.lower()
        explicit_artefact_intent = any(
            token in lowered
            for token in (
                "download",
                "ingest",
                "upload",
                "store",
                "save",
                "finalise",
                "finalize",
            )
        )
        if not explicit_artefact_intent:
            return None

        tool_name = (
            "finalise_cached_paper"
            if ("finalise" in lowered or "finalize" in lowered or "cached" in lowered)
            else "download_paper"
        )

        return [
            {
                "action": "call_tool",
                "tool": tool_name,
                "payload": {"arxiv_id": arxiv_id},
            }
        ]

    def _resolve_allowed_write_tools(
        self,
        *,
        prompt: str,
        requested_write_tools: list[str],
        recent_user_prompts: list[str] | None,
        llm_client: Any,
        model: str | None,
        user_namespace: str | None,
        auxiliary_system_prompt: str | None,
        trace: Any | None,
    ) -> tuple[set[str], str]:
        workflow_result = self.execute_workflow(
            WRITE_TOOL_POLICY_WORKFLOW_ID,
            data={
                "prompt": prompt,
                "requested_write_tools": list(requested_write_tools),
                "recent_user_prompts": list(recent_user_prompts or []),
            },
            llm_client=llm_client,
            model=model,
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
            trace=trace,
        )
        if workflow_result is None:
            return set(), "workflow_unavailable"
        allowed = workflow_result.data.get("allowed_write_tools")
        reason = workflow_result.data.get("write_policy_reason")
        allowed_set: set[str] = set()
        if isinstance(allowed, list):
            allowed_set = {
                str(item) for item in allowed if isinstance(item, str) and item
            }
        return allowed_set, str(reason or "")

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
        preferred_language: str | None = None,
        progress_tracker: ProgressTracker | None = None,
    ) -> OrchestratorResult:
        aux_llm_calls: List[Mapping[str, Any]] = []
        llm_calls: list[dict[str, Any]] = []
        orchestrator_start = time.perf_counter()
        recent_user_prompts = self._extract_recent_user_prompts(context, max_count=5)

        # JVNAUTOSCI-1038: Request-scoped progress helpers
        def _emit_progress_local(info: Mapping[str, Any]) -> None:
            """Route progress to tracker or fallback to instance method."""
            if progress_tracker is not None:
                progress_tracker.emit(info)
            else:
                self._emit_progress(info)

        def _emit_phase_transition_local(
            phase: str, *, extra: Mapping[str, Any] | None = None
        ) -> None:
            """Route phase transition to tracker or fallback to instance method."""
            if progress_tracker is not None:
                progress_tracker.transition_phase(phase, extra=extra)
            else:
                self._emit_phase_transition(phase, extra=extra)

        def _check_cancellation_local() -> None:
            """Check for cancellation and raise if requested.

            JVNAUTOSCI-1038: For background tasks, this allows graceful termination.
            """
            if progress_tracker is not None:
                progress_tracker.check_cancellation()

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

        def _record_llm_call(
            *,
            call_type: str,
            model_name: str | None,
            duration_ms: float | None,
            usage: Mapping[str, Any] | None = None,
            note: str | None = None,
            stage: str | None = None,
            provider: str | None = None,
            candidate: Mapping[str, Any] | None = None,
        ) -> None:
            payload: dict[str, Any] = {
                "type": call_type,
                "model": model_name,
                "provider": provider or _infer_provider(model_name),
                "duration_ms": duration_ms,
                "usage": dict(usage) if isinstance(usage, Mapping) else None,
                "workflow": "internal_mcp",
            }
            if isinstance(stage, str) and stage.strip():
                payload["stage"] = stage.strip()
            if isinstance(note, str) and note.strip():
                payload["note"] = note.strip()
            if isinstance(candidate, Mapping) and candidate:
                payload["candidate"] = dict(candidate)
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

        def _build_tool_call_validation_error_result(
            errors: Sequence[str],
            warnings: Sequence[str],
            tool_unavailable: Sequence[str],
            *,
            raw_tool_call: str | None,
            invocations_override: Sequence[Mapping[str, Any]] = (),
            tool_messages_override: Sequence[Mapping[str, Any]] = (),
        ) -> OrchestratorResult:
            payload: dict[str, Any] = {
                "errors": list(errors),
                "warnings": list(warnings),
                "tool_unavailable": list(tool_unavailable),
            }
            if raw_tool_call:
                payload["raw_tool_call"] = raw_tool_call[:8000]

            tool_invocations = list(invocations_override)
            tool_invocations.append(
                {
                    "tool": "__tool_call_validation_error__",
                    "payload": payload,
                    "error": "; ".join(errors) if errors else "validation_failed",
                }
            )

            message_lines = [
                "Tool call was not executed due to a validation error.",
            ]
            if tool_unavailable:
                message_lines.append(
                    "Unavailable tools: " + ", ".join(sorted(set(tool_unavailable)))
                )
            if errors:
                message_lines.append("Errors:")
                message_lines.extend([f"- {err}" for err in errors])
            if warnings:
                message_lines.append("Warnings:")
                message_lines.extend([f"- {warning}" for warning in warnings])

            message_lines.append(
                "\nPlease retry. If this keeps happening, share the debug output so we can reproduce it."
            )

            return OrchestratorResult(
                response_text="\n".join(message_lines),
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
                trace.metadata["llm_calls"] = list(llm_calls)
                usage_totals = _aggregate_usage_total()
                if usage_totals is not None:
                    trace.metadata["llm_usage"] = dict(usage_totals)
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

        policy_state, policy_telemetry = self._load_workflow_model_policy(
            preferred_language
        )
        if policy_telemetry:
            aux_llm_calls.append(policy_telemetry)
            if trace_enabled and trace is not None:
                trace.metadata["workflow_model_policy"] = dict(policy_telemetry)

        registry_snapshot: Mapping[str, Any] | None = None
        try:
            from ...services.model_registry_service import get_model_registry_snapshot

            registry_snapshot = get_model_registry_snapshot(
                preferred_language=preferred_language
            )
        except Exception:
            registry_snapshot = None

        if isinstance(registry_snapshot, Mapping):
            models_value = registry_snapshot.get("models")
            model_count = len(models_value) if isinstance(models_value, list) else None
            model_sample = models_value[:5] if isinstance(models_value, list) else None
            registry_summary = {
                "type": "model_registry",
                "source": registry_snapshot.get("source"),
                "model_count": model_count,
                "models": model_sample,
            }
            aux_llm_calls.append(registry_summary)
            if trace_enabled and trace is not None:
                trace.metadata["model_registry"] = dict(registry_summary)

        user_concept_id = user_namespace
        org_concept_id = None

        def _model_for_stage(stage: str) -> Optional[str]:
            return self._select_model_for_stage(
                stage=stage,
                default_model=model,
                policy_state=policy_state,
                registry_snapshot=registry_snapshot,
            )

        def _summarise_tool_messages_for_critic(
            messages: Sequence[Mapping[str, Any]],
        ) -> str:
            if not messages:
                return "(no tool calls)"

            lines: list[str] = []
            for msg in messages[:20]:
                content = msg.get("content") if isinstance(msg, Mapping) else None
                if not isinstance(content, str) or not content.strip():
                    continue
                snippet = content.strip().replace("\r\n", "\n")
                if len(snippet) > 800:
                    snippet = snippet[:800] + "\n... [truncated]"
                lines.append(snippet)
            return "\n\n".join(lines) if lines else "(no tool calls)"

        def _maybe_apply_critic(
            response_text: str,
            *,
            tool_messages_for_critic: Sequence[Mapping[str, Any]] = (),
        ) -> str:
            critic_enabled = os.getenv("VON_CRITIC_ENABLE", "0").lower() in {
                "1",
                "true",
            }
            if not critic_enabled:
                return response_text

            if "<screen>" in response_text or "<spoken>" in response_text:
                return response_text

            tool_summary = _summarise_tool_messages_for_critic(tool_messages_for_critic)
            critic_prompt = (
                "Review the assistant response for factual consistency and policy compliance. "
                'If it is acceptable, return JSON: {"approve": true}. '
                'If it needs correction, return JSON: {"approve": false, "revised_response": "...", "notes": "..."}. '
                "Return ONLY JSON."
            )
            critic_context = [
                {
                    "role": "system",
                    "content": "You are a strict reviewer. Use New Zealand English spelling.",
                },
                {
                    "role": "user",
                    "content": (
                        "User prompt:\n"
                        f"{prompt}\n\n"
                        "Assistant response:\n"
                        f"{response_text}\n\n"
                        "Tool outputs (authoritative):\n"
                        f"{tool_summary}\n"
                    ),
                },
            ]

            critic_response = None
            critic_model_used = _model_for_stage("critic")
            try:
                critic_response, critic_model_used, _ = self._run_llm_with_fallbacks(
                    stage="critic",
                    prompt=critic_prompt,
                    context=critic_context,
                    default_client=llm_client,
                    default_model=critic_model_used,
                    policy_state=policy_state,
                    registry_snapshot=registry_snapshot,
                    user_concept_id=user_concept_id,
                    org_concept_id=org_concept_id,
                    llm_calls_log=llm_calls,
                    aux_log=aux_llm_calls,
                    record_llm_call=_record_llm_call,
                )
            except Exception as exc:
                aux_llm_calls.append(
                    {
                        "type": "critic",
                        "stage": "critic",
                        "error": str(exc),
                        "model": critic_model_used,
                    }
                )
                return response_text

            try:
                parsed = json.loads(str(critic_response))
            except Exception:
                aux_llm_calls.append(
                    {
                        "type": "critic",
                        "stage": "critic",
                        "error": "critic_json_parse_failed",
                        "model": critic_model_used,
                    }
                )
                return response_text

            if isinstance(parsed, dict) and parsed.get("approve") is False:
                revised = parsed.get("revised_response")
                if isinstance(revised, str) and revised.strip():
                    aux_llm_calls.append(
                        {
                            "type": "critic",
                            "stage": "critic",
                            "model": critic_model_used,
                            "approved": False,
                            "notes": parsed.get("notes") if parsed else None,
                        }
                    )
                    return revised.strip()

            aux_llm_calls.append(
                {
                    "type": "critic",
                    "stage": "critic",
                    "model": critic_model_used,
                    "approved": True,
                }
            )
            return response_text

        if not self._gateway.enabled or self._max_tool_invocations <= 0:
            planner_model = _model_for_stage("planner")
            if trace_enabled and trace is not None:
                llm_step = trace.start_step(
                    "llm.generate",
                    inputs={
                        "prompt": prompt,
                        "model": planner_model or "default",
                        "gateway_enabled": bool(self._gateway.enabled),
                        "max_tool_invocations": int(self._max_tool_invocations),
                    },
                )
            response, planner_model, _ = self._run_llm_with_fallbacks(
                stage="planner",
                prompt=prompt,
                context=context,
                default_client=llm_client,
                default_model=planner_model,
                policy_state=policy_state,
                registry_snapshot=registry_snapshot,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
                llm_calls_log=llm_calls,
                aux_log=aux_llm_calls,
                record_llm_call=_record_llm_call,
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
            response_text = response if isinstance(response, str) else str(response)
            response_text = _maybe_apply_critic(response_text)
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

        preflight = self._build_ontology_preflight(prompt, preferred_language)
        if preflight.telemetry:
            aux_llm_calls.append(preflight.telemetry)
            if trace_enabled and trace is not None:
                trace.metadata["ontology_preflight"] = dict(preflight.telemetry)

        augmented_context = self._build_augmented_context(
            context,
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
            preflight_message=preflight.message,
            preferred_language=preferred_language,
        )

        base_prompt_telemetry = self._consume_base_system_prompt_telemetry()
        if base_prompt_telemetry:
            aux_llm_calls.append(base_prompt_telemetry)

        # If the user asks which TTS voice is being used, attach a small session
        # snapshot so the assistant can answer reliably without guessing.
        if self._prompt_requests_tts_voice(prompt):
            hint = self._build_client_capabilities_voice_hint()
            if isinstance(hint, str) and hint.strip():
                # Place immediately after the main instruction message.
                augmented_context.insert(1, {"role": "system", "content": hint.strip()})

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
                classifier_model = _model_for_stage("classifier")
                selector_selection = self._workflow_selector.select_workflow(
                    llm_client=llm_client,
                    model=classifier_model,
                    turn_text=prompt,
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
                    "policy_state": policy_state,
                    "default_model": _model_for_stage("narration"),
                    "registry_snapshot": registry_snapshot,
                    "user_concept_id": user_concept_id,
                    "org_concept_id": org_concept_id,
                    "aux_llm_calls": aux_llm_calls,
                    "record_llm_call": _record_llm_call,
                }

                narration_result = self.execute_workflow(
                    CHAT_NARRATION_WORKFLOW_ID,
                    data=narration_data,
                    llm_client=llm_client,
                    model=_model_for_stage("narration"),
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
        if policy_state.enabled and policy_state.policy:
            use_structured = True

        response = ""

        # JVNAUTOSCI-984: Emit tool_plan phase transition.
        _emit_phase_transition_local(self.PHASE_TOOL_PLAN)

        if use_structured:
            self._logger.debug("[mcp_orchestrator] Using structured tool calling path")
            try:
                tool_call_model = _model_for_stage("tool_call")
                if trace_enabled and trace is not None:
                    llm_step = trace.start_step(
                        "llm.generate_with_tools",
                        inputs={
                            "prompt": prompt,
                            "model": tool_call_model or "default",
                            "context_messages": len(augmented_context),
                        },
                    )
                tool_definitions = self._convert_mcp_tools_to_structured_definitions()
                llm_response, tool_call_model, _ = self._run_llm_with_tools_fallbacks(
                    stage="tool_call",
                    prompt=prompt,
                    context=augmented_context,
                    tool_definitions=tool_definitions,
                    default_client=llm_client,
                    default_model=tool_call_model,
                    policy_state=policy_state,
                    registry_snapshot=registry_snapshot,
                    user_concept_id=user_concept_id,
                    org_concept_id=org_concept_id,
                    llm_calls_log=llm_calls,
                    aux_log=aux_llm_calls,
                    record_llm_call=_record_llm_call,
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
                            "model": tool_call_model or "default",
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
            tool_call_model = _model_for_stage("tool_call")
            if trace_enabled and trace is not None:
                llm_step = trace.start_step(
                    "llm.generate",
                    inputs={
                        "prompt": prompt,
                        "model": tool_call_model or "default",
                        "context_messages": len(augmented_context),
                    },
                )
            response, tool_call_model, _ = self._run_llm_with_fallbacks(
                stage="tool_call",
                prompt=prompt,
                context=augmented_context,
                default_client=llm_client,
                default_model=tool_call_model,
                policy_state=policy_state,
                registry_snapshot=registry_snapshot,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
                llm_calls_log=llm_calls,
                aux_log=aux_llm_calls,
                record_llm_call=_record_llm_call,
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
                        ),
                        "model": tool_call_model or "default",
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
                "user_prompt": prompt,
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
                "classifier_model": _model_for_stage("classifier"),
                "policy_state": policy_state,
                "default_model": tool_call_model,
                "registry_snapshot": registry_snapshot,
                "user_concept_id": user_concept_id,
                "org_concept_id": org_concept_id,
            }
            if workflow_def is not None:
                recovery_model = _model_for_stage("tool_recovery")
                env = WorkflowEnvironment(
                    llm_client=llm_client,
                    gateway=self._gateway,
                    model=recovery_model,
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

        method_catalogue = self._gateway.describe_methods()
        tool_categories: dict[str, str] = {}
        for name, meta in method_catalogue.items():
            if not isinstance(meta, Mapping):
                continue
            category = meta.get("category")
            if isinstance(category, str):
                tool_categories[name] = category

        allowed_write_tools: set[str] = set()
        write_policy_reason = ""

        # Support chained tool calls up to max_tool_invocations limit (JVNAUTOSCI-699)
        iteration_count = 0
        current_response = response
        # Track if we're in the first iteration with structured tool calls already extracted
        first_iteration_structured = use_structured and has_valid_tool_call

        # JVNAUTOSCI-984: Emit tool_execute phase transition before the tool loop.
        _emit_phase_transition_local(
            self.PHASE_TOOL_EXECUTE,
            extra={
                "tool_calls_cap": int(self._max_tool_invocations),
                "tool_batch_cap": int(self._tool_batch_cap),
            },
        )

        while iteration_count < self._max_tool_invocations:
            # JVNAUTOSCI-1038: Check for cancellation at start of each tool iteration
            _check_cancellation_local()

            remaining_tool_calls: list[_ToolCallRequest] = []

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
                        "user_prompt": prompt,
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
                        "classifier_model": _model_for_stage("classifier"),
                        "policy_state": policy_state,
                        "default_model": tool_call_model,
                        "user_concept_id": user_concept_id,
                        "org_concept_id": org_concept_id,
                    }
                    if workflow_def is not None:
                        recovery_model = _model_for_stage("tool_recovery")
                        env = WorkflowEnvironment(
                            llm_client=llm_client,
                            gateway=self._gateway,
                            model=recovery_model,
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

            preflight = self._preflight_tool_calls(
                cast(list[_ToolCallRequest], tool_calls),
                method_catalogue,
                user_namespace=user_namespace,
                selected_gmail_profile=selected_gmail_profile,
            )
            if preflight.warnings:
                try:
                    aux_llm_calls.append(
                        {
                            "type": "tool_call_coercions",
                            "warnings": list(preflight.warnings),
                        }
                    )
                except Exception:
                    pass
            if preflight.errors:
                repaired_calls = None
                if method_catalogue:
                    repaired_calls = self._attempt_tool_call_repair(
                        current_response=(
                            current_response
                            if isinstance(current_response, str)
                            else str(current_response)
                        ),
                        errors=preflight.errors,
                        tool_list=sorted(method_catalogue.keys()),
                        llm_client=llm_client,
                        policy_state=policy_state,
                        default_model=tool_call_model,
                        registry_snapshot=registry_snapshot,
                        user_concept_id=user_concept_id,
                        org_concept_id=org_concept_id,
                        aux_llm_calls=aux_llm_calls,
                        llm_calls_log=llm_calls,
                        record_llm_call=_record_llm_call,
                    )

                if repaired_calls:
                    preflight = self._preflight_tool_calls(
                        repaired_calls,
                        method_catalogue,
                        user_namespace=user_namespace,
                        selected_gmail_profile=selected_gmail_profile,
                    )
                    if not preflight.errors:
                        tool_calls = repaired_calls

            if preflight.errors:
                result = _build_tool_call_validation_error_result(
                    preflight.errors,
                    preflight.warnings,
                    preflight.tool_unavailable,
                    raw_tool_call=(
                        current_response
                        if isinstance(current_response, str)
                        else str(current_response)
                    ),
                    invocations_override=tuple(invocations),
                    tool_messages_override=tuple(tool_messages),
                )
                _persist_trace(status="completed")
                return result

            # Enforce invocation limit across batched calls.
            remaining = self._max_tool_invocations - iteration_count
            if remaining <= 0:
                break
            batch_cap = max(1, int(getattr(self, "_tool_batch_cap", 4)))
            allowed = min(remaining, batch_cap)
            if len(tool_calls) > allowed:
                remaining_tool_calls = cast(
                    list[_ToolCallRequest], tool_calls[allowed:]
                )
                tool_calls = cast(list[_ToolCallRequest], tool_calls[:allowed])

            current_batch_size = len(tool_calls)

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
                if not isinstance(payload, dict):
                    payload = dict(payload)
                else:
                    payload = dict(payload)
                tool_request[self._PAYLOAD_FIELD] = payload

                # Safety: block write-category tools unless user explicitly requested a Vontology mutation.
                # This is intentionally enforced at execution time so it applies to both legacy and
                # structured tool-calling paths, including retry flows.
                tool_category = tool_categories.get(tool_name)
                if tool_category == "write":
                    if tool_name not in allowed_write_tools:
                        allowed_write_tools, write_policy_reason = (
                            self._resolve_allowed_write_tools(
                                prompt=prompt,
                                requested_write_tools=[tool_name],
                                recent_user_prompts=recent_user_prompts,
                                llm_client=llm_client,
                                model=model,
                                user_namespace=user_namespace,
                                auxiliary_system_prompt=auxiliary_system_prompt,
                                trace=trace if trace_enabled else None,
                            )
                        )

                if tool_category == "write" and tool_name not in allowed_write_tools:
                    message = (
                        f"Blocked write tool {tool_name!r}: the user request appears read-only. "
                        "If you intended to perform a write (Vontology changes or artefact storage), restate the request explicitly."
                    )
                    tool_payload = self._format_tool_result(
                        tool_name, None, None, "error", message
                    )

                    blocked_record: dict[str, Any] = {
                        "tool": tool_name,
                        "payload": dict(payload),
                        "error": message,
                        "blocked": True,
                    }
                    if write_policy_reason:
                        blocked_record["write_policy_reason"] = write_policy_reason
                    if call_id:
                        blocked_record["call_id"] = call_id
                    invocations.append(blocked_record)

                    if tool_step is not None:
                        try:
                            tool_step.finish_failed(message)
                        except Exception:
                            pass

                    _emit_progress_local(
                        {
                            "status": "tool_blocked",
                            "tool": tool_name,
                            "batch_size": current_batch_size,
                            "tool_calls_done": iteration_count,
                            "tool_calls_cap": int(self._max_tool_invocations),
                            "tool_calls_remaining": max(
                                0, int(self._max_tool_invocations) - iteration_count
                            ),
                            "call_id": call_id,
                            "error": message,
                        }
                    )

                    augmented_context.append({"role": "tool", "content": tool_payload})
                    tool_messages.append({"role": "tool", "content": tool_payload})
                    continue

                try:
                    schema = self._tool_schema_for_name(tool_name, method_catalogue)
                    self._apply_payload_defaults(
                        tool_name,
                        payload,
                        schema=schema,
                        user_namespace=user_namespace,
                        selected_gmail_profile=selected_gmail_profile,
                    )

                    if tool_name.startswith("gmail_"):
                        if payload.get("profile"):
                            self._logger.info(
                                "[mcp_orchestrator] Using gmail_profile=%s for tool=%s",
                                payload.get("profile"),
                                tool_name,
                            )
                        else:
                            self._logger.warning(
                                "[mcp_orchestrator] Gmail tool=%s invoked without profile and no default configured",
                                tool_name,
                            )
                    else:
                        if user_namespace and "namespace" in payload:
                            self._logger.info(
                                "[mcp_orchestrator] Using namespace=%s for tool=%s",
                                payload.get("namespace"),
                                tool_name,
                            )
                        elif user_namespace:
                            self._logger.warning(
                                "[mcp_orchestrator] No user_namespace available for tool=%s (unauthenticated request)",
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

                    _emit_progress_local(
                        {
                            "status": "tool_invoked",
                            "tool": tool_name,
                            "batch_size": current_batch_size,
                            "tool_calls_done": iteration_count,
                            "tool_calls_cap": int(self._max_tool_invocations),
                            "tool_calls_remaining": max(
                                0, int(self._max_tool_invocations) - iteration_count
                            ),
                            "call_id": call_id,
                        }
                    )

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

                    _emit_progress_local(
                        {
                            "status": "tool_failed",
                            "tool": tool_name,
                            "batch_size": current_batch_size,
                            "tool_calls_done": iteration_count,
                            "tool_calls_cap": int(self._max_tool_invocations),
                            "tool_calls_remaining": max(
                                0, int(self._max_tool_invocations) - iteration_count
                            ),
                            "call_id": call_id,
                            "error": str(exc),
                        }
                    )

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

            # If the model provided more tool calls than the batch cap, execute them
            # (up to max_tool_invocations) before asking for a final answer. This
            # prevents the assistant from prematurely narrating completion after only
            # the first batch executes (JVNAUTOSCI-941).
            if remaining_tool_calls and iteration_count < self._max_tool_invocations:
                current_response = json.dumps(remaining_tool_calls)
                continue

            follow_up_prompt = (
                "Provide a final answer to the user now that the tool result is available. "
                "If the tool failed, explain the error. "
                "If you need to call another tool, you may do so."
            )
            summariser_model = _model_for_stage("summariser")
            current_response, summariser_model, _ = self._run_llm_with_fallbacks(
                stage="summariser",
                prompt=follow_up_prompt,
                context=augmented_context,
                default_client=llm_client,
                default_model=summariser_model,
                policy_state=policy_state,
                registry_snapshot=registry_snapshot,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
                llm_calls_log=llm_calls,
                aux_log=aux_llm_calls,
                record_llm_call=_record_llm_call,
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
            # JVNAUTOSCI-984: Emit progress update for tool limit reached.
            _emit_progress_local(
                {
                    "status": "tool_limit_reached",
                    "tool_calls_done": iteration_count,
                    "tool_calls_cap": int(self._max_tool_invocations),
                    "tool_calls_remaining": 0,
                }
            )

        final_response_text = _maybe_apply_narration_routing(current_response)
        final_response_text = _maybe_apply_critic(
            final_response_text, tool_messages_for_critic=tool_messages
        )

        # JVNAUTOSCI-984: Emit completed phase transition.
        _emit_phase_transition_local(
            self.PHASE_COMPLETED,
            extra={
                "tool_calls_done": iteration_count,
                "tool_calls_cap": int(self._max_tool_invocations),
            },
        )

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
