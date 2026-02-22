"""Chat assistant orchestration utilities for the internal MCP gateway."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    MISSING_TOOL_CALL_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
)
from ...workflows.conversation_turn_stage_model import (
    build_conversation_turn_stage_model_snapshot,
    build_conversation_turn_stage_path,
)
from ...workflows.engine import WorkflowExecutor
from ...workflows.workflow_selector import WorkflowSelector
from ...workflows.durable.registry_factory import (
    build_workflow_registry,
    build_durable_action_registry,
)

from src.backend.workflows.write_tool_policy import (
    compute_allowed_write_tools,
    is_high_impact_vontology_write_tool,
    prompt_grants_high_impact_kb_write_approval,
    prompt_explicitly_denies_write,
)
from ...services.turn_execution_record_service import build_turn_execution_record
from src.backend.services.buttonify_service import (
    BUTTONIFY_PROMPT_IDS,
    BUTTONIFY_PROMPT_TEMPLATE,
    extract_buttonify_options_heuristic,
    parse_buttonify_options_json,
    dedupe_buttonify_options,
    sanitise_buttonify_heuristic_options,
    select_buttonify_preflight_options,
)

# Tool metadata service for Vontology-driven tool display (JVNAUTOSCI-1073)
from src.backend.services.tool_metadata_service import (
    get_tool_metadata,
    get_tool_salience,
    is_tool_visible,
)


@dataclass(frozen=True)
class WorkflowRoutingInfo:
    """Transparent record of how a chat turn was routed.

    JVNAUTOSCI-825: Provides traceable reasoning for the routing decision:
    which workflow was selected, what the classifier verdict was, which
    discovered workflows were candidates, and how long routing took.
    """

    workflow_id: str
    verdict: str
    prompt_id: str | None
    discovered_workflow_ids: tuple[str, ...]
    routing_duration_ms: float | None = None
    source: str = "selector"  # "selector" | "default" | "presenter_mode"


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
    # JVNAUTOSCI-825: Workflow routing transparency.
    workflow_routing: WorkflowRoutingInfo | None = None
    # JVNAUTOSCI-1140: Optional renderer routing/render-mode diagnostics.
    # Present only when renderer applicability routing is enabled and evaluated.
    render_plan: Mapping[str, Any] | None = None


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
        "buttonify": "Generating quick replies",
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
    _PREFLIGHT_TYPE_TYPE_ID = "#V#conversation_preflight_type"
    _PREFLIGHT_CACHE_TTL_SECONDS = 120
    _TOPIC_VOCABULARY_CACHE_TTL_SECONDS = 180
    _TOPIC_VOCABULARY_MAX_TYPES = 15
    _TOPIC_VOCABULARY_MAX_PREDICATES = 12
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
    PHASE_BUTTONIFY = "buttonify"
    PHASE_COMPLETED = "completed"
    PHASE_ERROR = "error"

    _PHASE_LABELS: Mapping[str, str] = {
        PHASE_CONTEXT_BUILD: "Building context",
        PHASE_TOOL_PLAN: "Planning tool calls",
        PHASE_TOOL_EXECUTE: "Executing tools",
        PHASE_SCREEN_BACKFILL: "Generating response",
        PHASE_NARRATION: "Generating narration",
        PHASE_BUTTONIFY: "Generating quick replies",
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
    # Completion-claim validation guardrails (JVNAUTOSCI-940).
    # Keep this bounded and deterministic so we avoid extra LLM traffic while
    # still surfacing when the assistant claims work is already complete.
    _COMPLETION_CLAIM_MAX_CANDIDATES = 8
    _COMPLETION_CLAIM_MAX_TEXT_CHARS = 16_000
    _COMPLETION_CLAIM_VERB_PATTERN = re.compile(
        r"\b(i|we)\s+(?:have\s+|had\s+)?(?:successfully\s+)?"
        r"(completed|finished|implemented|created|added|updated|deleted|removed|linked|"
        r"merged|pushed|committed|commented|transitioned|assigned|validated|verified|"
        r"executed|ran|invoked|called|retrieved|fetched|searched|resolved|fixed|closed)\b",
        flags=re.IGNORECASE,
    )
    _COMPLETION_CLAIM_LINE_START_PATTERN = re.compile(
        r"^(completed|finished|implemented|created|added|updated|deleted|removed|"
        r"linked|merged|pushed|committed|commented|transitioned|assigned|validated|"
        r"verified|executed|ran|invoked|called|retrieved|fetched|searched|"
        r"resolved|fixed|closed)\b",
        flags=re.IGNORECASE,
    )
    _COMPLETION_CLAIM_INTENT_PATTERN = re.compile(
        r"\b(i will|i'll|we will|we'll|i am going to|i'm going to|we are going to|"
        r"we're going to|let me|about to)\b",
        flags=re.IGNORECASE,
    )
    # Minimal-imposition auto-proceed gate:
    # If the assistant explicitly says it is continuing now (for example
    # "Proceeding now") and it is not asking the user for a decision, we
    # should continue the tool workflow instead of waiting for another human
    # prompt.
    _AUTO_PROCEED_PROGRESS_PROMISE_PATTERN = re.compile(
        r"\b(i will now|i'll now|i am going to|i'm going to|proceeding now|"
        r"next (?:i|we) will|continuing now|proceed with)\b",
        flags=re.IGNORECASE,
    )
    _AUTO_PROCEED_USER_DECISION_PATTERN = re.compile(
        r"\b(would you like|do you want|should i|shall i|can i|may i|"
        r"please confirm|confirm first|let me know|which option|"
        r"choose|select|pick|if you'd like|if you would like|if you want|"
        r"if you prefer)\b",
        flags=re.IGNORECASE,
    )
    _PROMPT_EXPLICIT_TOOL_CALL_PATTERN = re.compile(
        r"\bcall\s+`?([a-z_][a-z0-9_]*)`?\b",
        flags=re.IGNORECASE,
    )
    # Deterministic concept-id fields for write-category tools.
    #
    # We normalise these before execution so writable operations are robust to
    # close-but-wrong guessed IDs (for example #V#gill_dobbie vs
    # #V#gillian_dobbie) without relying on non-deterministic LLM retries.
    _WRITE_TOOL_CONCEPT_ID_FIELDS: Mapping[str, tuple[str, ...]] = {
        "add_relationship": ("source_id", "predicate", "target"),
        "remove_relationship": ("source_id", "predicate", "target"),
        "add_names": ("concept_id",),
        "update_concept": ("concept_id",),
        "delete_concept": ("concept_id",),
        "merge_concepts": ("source_id", "target_id"),
        "upsert_text_relation": ("concept_id",),
        "upsert_singleton_text_relation": ("concept_id",),
        "delete_text_relation": ("concept_id",),
        "update_text_relation": ("concept_id",),
        "create_concepts": ("parent_id",),
    }
    _HIGH_IMPACT_REVIEW_REASON = "high_impact_kb_write_requires_human_review"
    _HIGH_IMPACT_NAMESPACE_REASON = "high_impact_kb_write_requires_namespace"

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
        # Per-turn retry budget for missing-tool-call recovery.
        # Keep this bounded to avoid retry thrashing when multiple malformed
        # tool-call responses occur in the same turn.
        self._max_missing_tool_call_retries_per_turn = self._coerce_int(
            None,
            env_var="VON_MCP_MAX_MISSING_TOOL_CALL_RETRIES_PER_TURN",
            default=2,
            min_value=0,
            max_value=20,
        )
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

        # Topic vocabulary cache for context-based discovery (JVNAUTOSCI-1052).
        # Key: hash of extracted topic keywords; Value: {timestamp, types, predicates}.
        self._topic_vocabulary_cache: dict[str, dict[str, Any]] = {}

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
        # Unified registries: single source of truth for both conversation-turn
        # and durable workflows/actions.  See JVNAUTOSCI-922 Phase 1.
        self._workflow_registry = build_workflow_registry()
        self._action_registry = self._build_action_registry()
        self._workflow_executor = WorkflowExecutor(
            registry=self._action_registry, max_transitions=50
        )
        self._workflow_selector = WorkflowSelector(
            registry=self._workflow_registry,
            prompt_service=self._prompt_templates,
            verdict_mapping={
                "plain_response": CHAT_ASSISTANT_WORKFLOW_ID,
                "tool_seeking": TOOL_CALLING_WORKFLOW_ID,
                "summarisation": TOOL_CALLING_WORKFLOW_ID,
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
        max_missing_tool_call_retries_per_turn: int | None = None,
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

        if max_missing_tool_call_retries_per_turn is not None:
            self._max_missing_tool_call_retries_per_turn = self._coerce_non_negative_int(
                max_missing_tool_call_retries_per_turn,
                default=self._max_missing_tool_call_retries_per_turn,
                max_value=20,
            )

    def get_execution_caps(self) -> dict[str, int]:
        return {
            "max_tool_invocations": int(self._max_tool_invocations),
            "tool_batch_cap": int(self._tool_batch_cap),
            "max_missing_tool_call_retries_per_turn": int(
                self._max_missing_tool_call_retries_per_turn
            ),
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

    @staticmethod
    def _coerce_non_negative_int(
        value: Any,
        *,
        default: int = 0,
        max_value: int | None = None,
    ) -> int:
        """Coerce arbitrary numeric-like input into a bounded non-negative int."""
        try:
            coerced = int(value)
        except Exception:
            coerced = int(default)
        coerced = max(0, coerced)
        if max_value is not None:
            coerced = min(int(max_value), coerced)
        return coerced

    def _build_action_registry(self) -> ActionRegistry:
        """Build a unified action registry for all workflow execution.

        Starts from the durable action registry (rag_sync, considerations)
        and adds conversation-turn handlers (narration, missing-tool-call,
        write-policy, todo-refresh).  The result is a single registry usable
        by both WorkflowExecutor and DurableWorkflowExecutor.

        See JVNAUTOSCI-922 Phase 1.1 — unified ActionRegistry.
        """
        # Start with durable handlers as the base.
        registry = build_durable_action_registry()

        # Conversation-turn handlers — these reference ``self`` methods.
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
        registry.register(
            ActionSpec(
                action_id="buttonify.assess_input",
                handler=self._action_buttonify_assess_input,
                description="Assess whether buttonify output transformation should run.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="buttonify.select_prompt",
                handler=self._action_buttonify_select_prompt,
                description="Resolve buttonify prompt from Vontology with fallback template.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="buttonify.extract_options",
                handler=self._action_buttonify_extract_options,
                description="Extract quick-reply options via heuristic/model/fallback chain.",
            )
        )
        # Todo-refresh workflow actions (JVNAUTOSCI-922 Phase 2.3).
        registry.register(
            ActionSpec(
                action_id="todo_refresh.check_cache",
                handler=self._action_todo_refresh_check_cache,
                description="Check task cache freshness; skip Gmail if recent.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="todo_refresh.fetch_gmail",
                handler=self._action_todo_refresh_fetch_gmail,
                description="Fetch recent Gmail messages via MCP gateway.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="todo_refresh.extract_tasks",
                handler=self._action_todo_refresh_extract_tasks,
                description="LLM extraction of actionable tasks from emails.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="todo_refresh.prioritise",
                handler=self._action_todo_refresh_prioritise,
                description="Assign priority to extracted tasks via LLM.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="todo_refresh.summarise",
                handler=self._action_todo_refresh_summarise,
                description="Persist tasks and produce human-readable summary.",
            )
        )

        registry.register(
            ActionSpec(
                action_id="write_policy.decide",
                handler=self._action_write_policy_decide,
                description="Decide which write-category tools are allowed for this prompt.",
            )
        )
        # Tool-calling workflow actions (JVNAUTOSCI-922 Phase 2).
        # Real handlers that wrap the procedural tool-calling logic;
        # driven by #V#tool_calling_workflow state machine.
        registry.register(
            ActionSpec(
                action_id="tool_calling.plan",
                handler=self._action_tool_calling_plan,
                description="Initial LLM call + missing-tool-call recovery.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="tool_calling.validate",
                handler=self._action_tool_calling_validate,
                description="Preflight validation, coercion, and repair.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="tool_calling.execute",
                handler=self._action_tool_calling_execute,
                description="Execute tool-call batch against MCP gateway.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="tool_calling.backfill",
                handler=self._action_tool_calling_backfill,
                description="Summariser LLM call; detect chained tool calls.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="turn_execution.critic",
                handler=self._action_turn_execution_critic,
                description="Evaluate required effects and postcondition checks for the turn.",
            )
        )
        registry.register(
            ActionSpec(
                action_id="turn_execution.completion_gate",
                handler=self._action_turn_execution_completion_gate,
                description="Apply completion gate and prevent false completion claims.",
            )
        )

        # JVNAUTOSCI-922 Phase 3.1: Fallback handler for Vontology-defined
        # MCP tool actions.  Any action_id not explicitly registered is
        # routed through the MCP gateway via _action_mcp_tool_invoke.
        registry.set_fallback_handler(self._action_mcp_tool_invoke)
        return registry

    # -------------------------------------------------------------------
    # Generic MCP tool invocation action (Phase 3.1)
    # -------------------------------------------------------------------

    def _action_mcp_tool_invoke(self, request: Any) -> WorkflowActionResult:
        """Invoke an MCP tool by name via the gateway.

        JVNAUTOSCI-922 Phase 3.1: Generic fallback handler for
        Vontology-defined workflow steps whose ``invokes_action`` points
        to an MCP tool name rather than a registered Python handler.

        The ``action_id`` on the request is used as the MCP tool name.
        ``request.inputs`` (populated from the step's hasInputMap) are
        merged with defaults (namespace, gmail_profile) and passed as the
        gateway payload.

        Outputs on success:
            mcp_result (dict): The raw payload returned by the gateway.
            mcp_tool (str): The tool name that was invoked.
            mcp_duration_ms (float): Execution time in milliseconds.
            result (Any): Alias of mcp_result for condition evaluation.
        """
        tool_name = request.action_id
        env = request.environment
        gateway = self._gateway

        if gateway is None or not gateway.enabled:
            return WorkflowActionResult(
                status="failed",
                error=f"gateway_unavailable_for:{tool_name}",
            )

        # Build payload from inputs + context overrides.
        payload: dict[str, Any] = dict(request.inputs) if request.inputs else {}

        # Apply standard defaults (namespace, gmail_profile, session_id).
        try:
            method_catalogue = gateway.describe_methods()
            schema = self._tool_schema_for_name(tool_name, method_catalogue)
        except Exception:
            schema = None

        self._apply_payload_defaults(
            tool_name,
            payload,
            schema=schema,
            user_namespace=env.user_namespace,
            selected_gmail_profile=env.default_gmail_profile,
            conversation_session_id=request.data.get("conversation_session_id"),
            turn_id=request.data.get("turn_id"),
        )

        try:
            result = gateway.invoke(tool_name, payload)
            try:
                from ...workflows.workflow_baseline_telemetry import (
                    record_generic_fallback_mcp_invocation,
                )

                record_generic_fallback_mcp_invocation(success=True)
            except Exception:
                pass
            return WorkflowActionResult(
                status="success",
                outputs={
                    "mcp_result": result.payload,
                    "mcp_tool": tool_name,
                    "mcp_duration_ms": result.duration_ms,
                    "result": result.payload,
                },
                duration_ms=result.duration_ms,
            )
        except Exception as exc:
            try:
                from ...workflows.workflow_baseline_telemetry import (
                    record_generic_fallback_mcp_invocation,
                )

                record_generic_fallback_mcp_invocation(success=False)
            except Exception:
                pass
            self._logger.warning(
                "[mcp_orchestrator] Generic MCP invoke failed for %s: %s",
                tool_name,
                exc,
            )
            return WorkflowActionResult(
                status="failed",
                error=f"mcp_invoke_failed:{tool_name}:{exc}",
            )

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

        retry_attempts = self._coerce_non_negative_int(
            data.get("missing_tool_call_retry_attempts"),
            default=0,
            max_value=20,
        )
        retry_budget = self._coerce_non_negative_int(
            data.get("missing_tool_call_retry_budget"),
            default=self._max_missing_tool_call_retries_per_turn,
            max_value=20,
        )
        retries_remaining_before = max(0, retry_budget - retry_attempts)
        data["missing_tool_call_retry_attempts"] = retry_attempts
        data["missing_tool_call_retry_budget"] = retry_budget

        override_retry_reason_raw = data.get("missing_tool_call_retry_reason_override")
        override_retry_reason = (
            override_retry_reason_raw.strip()
            if isinstance(override_retry_reason_raw, str)
            else ""
        )
        if override_retry_reason:
            interpretation = data.get("interpretation")
            use_structured = bool(data.get("use_structured"))
            response_text = (
                data.get("response_text", "")
                if isinstance(data.get("response_text", ""), str)
                else str(data.get("response_text", ""))
            )
            is_json_action = (
                interpretation.is_json_action
                if (
                    not use_structured
                    and isinstance(interpretation, _ModelTurnInterpretation)
                )
                else self._is_json_action_response(response_text)
            )
            fenced_detected = (
                interpretation.fenced_tool_call_json
                if (
                    not use_structured
                    and isinstance(interpretation, _ModelTurnInterpretation)
                )
                else self._contains_fenced_tool_call_json(response_text)
            )
            assessment = _MissingToolCallAssessment(
                path="structured" if use_structured else "legacy",
                is_json_action=bool(is_json_action),
                fenced_json=bool(fenced_detected),
                classifier_invoked=False,
                classifier_has_verdict=False,
                classifier_used=False,
                classifier_verdict=None,
                retry_reason=override_retry_reason,
                tool_call_parse_error=data.get("tool_call_parse_error"),
            )
        else:
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
                    "retry_reason_override_applied": bool(override_retry_reason),
                    "parse_error": (
                        str(assessment.tool_call_parse_error)
                        if assessment.tool_call_parse_error is not None
                        else ""
                    ),
                    "retry_attempts": retry_attempts,
                    "retry_budget": retry_budget,
                    "retries_remaining": retries_remaining_before,
                }
            )
        except Exception:
            pass

        retry_needed = bool(assessment.retry_reason) and retries_remaining_before > 0
        retry_suppressed = bool(assessment.retry_reason) and not retry_needed
        if retry_suppressed:
            try:
                aux_log.append(
                    {
                        "type": "missing_tool_call_retry",
                        "mechanism": "budget",
                        "stage": "skipped",
                        "retry_reason": assessment.retry_reason or "",
                        "retry_attempts": retry_attempts,
                        "retry_budget": retry_budget,
                        "retries_remaining": retries_remaining_before,
                    }
                )
            except Exception:
                pass

        outputs = {
            "missing_tool_call_assessment": asdict(assessment),
            "missing_tool_call_retry_needed": retry_needed,
            "missing_tool_call_retry_reason": assessment.retry_reason,
            "missing_tool_call_retry_attempts": retry_attempts,
            "missing_tool_call_retry_budget": retry_budget,
            "missing_tool_call_retry_remaining": retries_remaining_before,
            "missing_tool_call_retry_suppressed": retry_suppressed,
            "tool_call_parse_error": data.get("tool_call_parse_error"),
            "result": retry_needed,
        }

        if request.trace is not None:
            request.trace.metadata.setdefault("missing_tool_call", {})
            request.trace.metadata["missing_tool_call"].update(
                {
                    "retry_reason": assessment.retry_reason,
                    "classifier_verdict": assessment.classifier_verdict,
                    "retry_attempts": retry_attempts,
                    "retry_budget": retry_budget,
                    "retries_remaining": retries_remaining_before,
                    "retry_suppressed": retry_suppressed,
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
                    try:
                        from ...workflows.workflow_baseline_telemetry import (
                            record_write_policy_decision,
                        )

                        record_write_policy_decision(
                            stage="write_policy.decide",
                            allowed_tools_count=len(allowed),
                        )
                    except Exception:
                        pass
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
        try:
            from ...workflows.workflow_baseline_telemetry import (
                record_write_policy_decision,
            )

            record_write_policy_decision(
                stage="write_policy.decide",
                allowed_tools_count=len(decision.allowed_tools),
            )
        except Exception:
            pass

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
        missing_required_tools = data.get("missing_prompt_tools")
        if not isinstance(missing_required_tools, list):
            missing_required_tools = []
        missing_required_tools = [
            str(item).strip()
            for item in missing_required_tools
            if isinstance(item, str) and item.strip()
        ]
        missing_required_fetch_concept_ids = data.get("missing_prompt_fetch_concept_ids")
        if not isinstance(missing_required_fetch_concept_ids, list):
            missing_required_fetch_concept_ids = []
        missing_required_fetch_concept_ids = [
            str(item).strip()
            for item in missing_required_fetch_concept_ids
            if isinstance(item, str) and item.strip()
        ]
        required_create_type_name_raw = data.get("required_prompt_create_type_name")
        required_create_type_name = (
            str(required_create_type_name_raw).strip()
            if isinstance(required_create_type_name_raw, str)
            else None
        )
        emit_progress_raw = data.get("emit_progress")
        emit_progress_cb: Callable[[Mapping[str, Any]], None] | None = (
            cast(Callable[[Mapping[str, Any]], None], emit_progress_raw)
            if callable(emit_progress_raw)
            else None
        )
        record_llm_call = data.get("record_llm_call")
        llm_calls_log = data.get("llm_calls")
        calling_path = "legacy"
        assessment = data.get("missing_tool_call_assessment")
        if isinstance(assessment, dict) and isinstance(assessment.get("path"), str):
            calling_path = str(assessment.get("path") or "legacy")

        retry_attempts = self._coerce_non_negative_int(
            data.get("missing_tool_call_retry_attempts"),
            default=0,
            max_value=20,
        )
        retry_budget = self._coerce_non_negative_int(
            data.get("missing_tool_call_retry_budget"),
            default=self._max_missing_tool_call_retries_per_turn,
            max_value=20,
        )
        retries_remaining_before = max(0, retry_budget - retry_attempts)
        data["missing_tool_call_retry_attempts"] = retry_attempts
        data["missing_tool_call_retry_budget"] = retry_budget

        if retries_remaining_before <= 0:
            if emit_progress_cb is not None:
                emit_progress_cb(
                    {
                        "status": "retry_end",
                        "stage": "tool_recovery",
                        "retry_attempts": retry_attempts,
                        "retry_budget": retry_budget,
                        "retry_remaining": 0,
                        "success": False,
                        "skipped": True,
                    }
                )
            try:
                aux_log.append(
                    {
                        "type": "missing_tool_call_retry",
                        "path": calling_path,
                        "mechanism": "budget",
                        "stage": "skipped",
                        "retry_reason": data.get("missing_tool_call_retry_reason")
                        or "",
                        "retry_attempts": retry_attempts,
                        "retry_budget": retry_budget,
                        "retries_remaining": retries_remaining_before,
                    }
                )
            except Exception:
                pass
            return WorkflowActionResult(
                outputs={
                    "response_text": data.get("response_text"),
                    "tool_calls": data.get("tool_calls"),
                    "missing_tool_call_retry_success": False,
                    "tool_call_parse_error": data.get("tool_call_parse_error"),
                    "missing_tool_call_retry_attempts": retry_attempts,
                    "missing_tool_call_retry_budget": retry_budget,
                    "missing_tool_call_retry_remaining": 0,
                    "missing_tool_call_retry_suppressed": True,
                    "result": False,
                },
                duration_ms=0.0,
            )

        retry_attempts += 1
        retries_remaining_after = max(0, retry_budget - retry_attempts)
        data["missing_tool_call_retry_attempts"] = retry_attempts

        if emit_progress_cb is not None:
            emit_progress_cb(
                {
                    "status": "retry_start",
                    "stage": "tool_recovery",
                    "retry_attempts": retry_attempts,
                    "retry_budget": retry_budget,
                    "retry_remaining": retries_remaining_after,
                    "retry_reason": data.get("missing_tool_call_retry_reason") or "",
                }
            )

        forced = self._infer_missing_tool_call_retry_tool_calls(
            augmented_context,
            user_prompt=data.get("user_prompt"),
            missing_required_tools=missing_required_tools,
            missing_required_fetch_concept_ids=missing_required_fetch_concept_ids,
            required_create_type_name=required_create_type_name,
        )
        if forced:
            import json
            forced_mechanism = (
                "required_tools" if missing_required_tools else "heuristic"
            )

            try:
                aux_log.append(
                    {
                        "type": "missing_tool_call_retry",
                        "path": calling_path,
                        "mechanism": forced_mechanism,
                        "stage": "response",
                        "retry_reason": data.get("missing_tool_call_retry_reason")
                        or "",
                        "retry_attempts": retry_attempts,
                        "retry_budget": retry_budget,
                        "retries_remaining": retries_remaining_after,
                        "response_preview": "(forced tool call)",
                    }
                )
            except Exception:
                pass

            response_text = (
                json.dumps(forced[0]) if len(forced) == 1 else json.dumps(forced)
            )
            if emit_progress_cb is not None:
                emit_progress_cb(
                    {
                        "status": "retry_end",
                        "stage": "tool_recovery",
                        "retry_attempts": retry_attempts,
                        "retry_budget": retry_budget,
                        "retry_remaining": retries_remaining_after,
                        "success": True,
                        "result_summary": "forced_tool_call",
                    }
                )

            return WorkflowActionResult(
                outputs={
                    "response_text": response_text,
                    "tool_calls": forced,
                    "missing_tool_call_retry_success": True,
                    "tool_call_parse_error": None,
                    "missing_tool_call_retry_attempts": retry_attempts,
                    "missing_tool_call_retry_budget": retry_budget,
                    "missing_tool_call_retry_remaining": retries_remaining_after,
                    "missing_tool_call_retry_suppressed": False,
                    "result": True,
                },
                duration_ms=0.0,
            )

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
                    "retry_attempts": retry_attempts,
                    "retry_budget": retry_budget,
                    "retries_remaining": retries_remaining_after,
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
                llm_calls_log=(
                    llm_calls_log
                    if isinstance(llm_calls_log, list)
                    else data.get("llm_calls_log") or []
                ),
                aux_log=aux_log,
                record_llm_call=record_llm_call,
                emit_progress=emit_progress_cb,
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
                    "retry_attempts": retry_attempts,
                    "retry_budget": retry_budget,
                    "retries_remaining": retries_remaining_after,
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
            "missing_tool_call_retry_attempts": retry_attempts,
            "missing_tool_call_retry_budget": retry_budget,
            "missing_tool_call_retry_remaining": retries_remaining_after,
            "missing_tool_call_retry_suppressed": False,
            "result": success,
        }

        if emit_progress_cb is not None:
            emit_progress_cb(
                {
                    "status": "retry_end",
                    "stage": "tool_recovery",
                    "retry_attempts": retry_attempts,
                    "retry_budget": retry_budget,
                    "retry_remaining": retries_remaining_after,
                    "success": bool(success),
                    "duration_ms": int(duration_ms),
                    "tool_calls_found": bool(retry_calls),
                }
            )

        return WorkflowActionResult(
            outputs=outputs,
            duration_ms=duration_ms,
        )

    def _run_missing_tool_call_recovery_workflow(
        self,
        *,
        request: Any,
        environment: WorkflowEnvironment,
        data: Mapping[str, Any],
        response_text: str,
        interpretation: _ModelTurnInterpretation | None,
        use_structured: bool,
        tool_call_parse_error: ToolCallParsingError | None,
        tool_calls: Sequence[Mapping[str, Any]] | None,
        default_model: str | None,
    ) -> Mapping[str, Any] | None:
        """Execute the shared missing-tool-call recovery workflow.

        This keeps plan/backfill recovery behaviour aligned and avoids
        duplicating workflow bootstrap logic.
        """

        workflow_def = self._workflow_registry.get(MISSING_TOOL_CALL_WORKFLOW_ID)
        if workflow_def is None:
            return None

        model_for_stage = data.get("model_for_stage")
        if not callable(model_for_stage):
            return None

        workflow_context = {
            "user_prompt": data.get("prompt") or "",
            "response_text": response_text if isinstance(response_text, str) else str(response_text),
            "interpretation": interpretation,
            "use_structured": use_structured,
            "tool_call_parse_error": tool_call_parse_error,
            "aux_llm_calls": data.get("aux_llm_calls") or [],
            "augmented_context": data.get("augmented_context") or [],
            "tool_calls": tool_calls,
            "missing_tool_assessor": self._assess_missing_tool_call,
            "extract_tool_calls_fn": self._extract_tool_calls,
            "record_llm_call": data.get("record_llm_call"),
            "classifier_model": model_for_stage("classifier"),
            "policy_state": data.get("policy_state"),
            "default_model": default_model,
            "registry_snapshot": data.get("registry_snapshot"),
            "user_concept_id": data.get("user_concept_id"),
            "org_concept_id": data.get("org_concept_id"),
            "missing_tool_call_retry_attempts": data.get(
                "missing_tool_call_retry_attempts"
            ),
            "missing_tool_call_retry_budget": data.get("missing_tool_call_retry_budget"),
            "required_prompt_tools": data.get("required_prompt_tools"),
            "required_prompt_fetch_concept_ids": data.get(
                "required_prompt_fetch_concept_ids"
            ),
            "required_prompt_create_type_name": data.get(
                "required_prompt_create_type_name"
            ),
            "missing_prompt_tools": data.get("missing_prompt_tools"),
            "missing_prompt_fetch_concept_ids": data.get(
                "missing_prompt_fetch_concept_ids"
            ),
            "missing_tool_call_retry_reason_override": data.get(
                "missing_tool_call_retry_reason_override"
            ),
            "emit_progress": data.get("emit_progress"),
        }

        recovery_model_raw = model_for_stage("tool_recovery")
        recovery_model = (
            recovery_model_raw if isinstance(recovery_model_raw, str) else None
        )
        recovery_env = WorkflowEnvironment(
            llm_client=environment.llm_client,
            gateway=self._gateway,
            model=recovery_model,
            user_namespace=environment.user_namespace,
            auxiliary_system_prompt=environment.auxiliary_system_prompt,
            max_tool_invocations=environment.max_tool_invocations,
            default_gmail_profile=(
                data.get("gmail_profile") or environment.default_gmail_profile
            ),
        )
        workflow_context.setdefault(
            "conversation_session_id", data.get("conversation_session_id")
        )
        workflow_context.setdefault("turn_id", data.get("turn_id"))
        workflow_context.setdefault("workflow_episode_source", "chat_turn_workflow")
        workflow_context.setdefault("workflow_episode_stage", "missing_tool_recovery")

        workflow_result = self.execute_workflow(
            MISSING_TOOL_CALL_WORKFLOW_ID,
            data=workflow_context,
            llm_client=environment.llm_client,
            model=recovery_model,
            user_namespace=environment.user_namespace,
            auxiliary_system_prompt=environment.auxiliary_system_prompt,
            trace=request.trace,
            environment=recovery_env,
            conversation_session_id=(
                data.get("conversation_session_id")
                if isinstance(data.get("conversation_session_id"), str)
                else None
            ),
            turn_id=data.get("turn_id") if isinstance(data.get("turn_id"), str) else None,
            episode_source="chat_turn_workflow",
        )
        if workflow_result is None:
            return None
        return workflow_result.data

    def _action_narration_classify(self, request: Any) -> WorkflowActionResult:
        required = bool(
            request.data.get("presenter_mode_requested")
            or request.data.get("force_narration")
        )
        return WorkflowActionResult(
            outputs={
                "narration_required": required,
                "result": required,
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
        emit_progress_raw = request.data.get("emit_progress")
        emit_progress_cb: Callable[[Mapping[str, Any]], None] | None = (
            cast(Callable[[Mapping[str, Any]], None], emit_progress_raw)
            if callable(emit_progress_raw)
            else None
        )
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
                emit_progress=emit_progress_cb,
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
            "result": bool(spoken),
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

    @staticmethod
    def _build_output_transformation_contract(
        *,
        transform_name: str,
        transform_version: str,
        workflow_id: str,
        stage_id: str,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        diagnostics: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build a reusable output-transformation workflow result envelope."""
        return {
            "schema_version": "output_transformation_workflow_contract_v1",
            "transform_name": transform_name,
            "transform_version": transform_version,
            "stage_metadata": {
                "workflow_id": workflow_id,
                "stage_id": stage_id,
            },
            "input_payload": dict(input_payload),
            "output_payload": dict(output_payload),
            "diagnostics": dict(diagnostics),
        }

    def _action_buttonify_assess_input(self, request: Any) -> WorkflowActionResult:
        buttonify_enabled = bool(request.data.get("buttonify_enabled"))
        buttonify_allowed = bool(request.data.get("buttonify_allowed"))
        screen_text = request.data.get("screen_text")

        suppression_reason: str | None = None
        should_run = True
        if not buttonify_enabled:
            should_run = False
            suppression_reason = "buttonify_disabled"
        elif not buttonify_allowed:
            should_run = False
            suppression_reason = "buttonify_not_allowed"
        elif not isinstance(screen_text, str) or not screen_text.strip():
            should_run = False
            suppression_reason = "empty_response"

        return WorkflowActionResult(
            outputs={
                "buttonify_should_run": should_run,
                "buttonify_status": "no_op" if should_run else "skipped",
                "buttonify_source": "none",
                "buttonify_suppression_reason": suppression_reason,
                "buttonify_error_class": None,
                "buttonify_model_attempted": False,
                "buttonify_model_used": request.data.get("default_model")
                or request.environment.model,
                "buttonify_options": [],
            }
        )

    def _action_buttonify_select_prompt(self, request: Any) -> WorkflowActionResult:
        prompt_ids_raw = request.data.get("buttonify_prompt_ids")
        prompt_ids = tuple(
            item.strip()
            for item in (
                prompt_ids_raw
                if isinstance(prompt_ids_raw, (list, tuple))
                else BUTTONIFY_PROMPT_IDS
            )
            if isinstance(item, str) and item.strip()
        )
        if not prompt_ids:
            prompt_ids = BUTTONIFY_PROMPT_IDS

        fallback_template = request.data.get("buttonify_prompt_template")
        if not isinstance(fallback_template, str) or not fallback_template.strip():
            fallback_template = BUTTONIFY_PROMPT_TEMPLATE

        variables = {
            "user_message": request.data.get("user_prompt") or "",
            "assistant_response": request.data.get("screen_text") or "",
        }
        rendered = None
        try:
            rendered = self._prompt_templates.render_prompt(
                prompt_ids,
                fallback=fallback_template,
                variables=variables,
                max_chars=6000,
            )
        except Exception:
            rendered = None

        if rendered:
            prompt_text = rendered.text
            prompt_id = rendered.prompt_id
            prompt_truncated = rendered.truncated
        else:
            try:
                prompt_text = fallback_template.format(**variables)
            except Exception:
                prompt_text = str(fallback_template)
            prompt_id = None
            prompt_truncated = False

        if request.trace is not None and prompt_text:
            request.trace.record_prompt(
                prompt_id=prompt_id,
                resolved_prompt=prompt_text,
                variables=variables,
            )

        return WorkflowActionResult(
            outputs={
                "buttonify_prompt_text": prompt_text,
                "buttonify_prompt_id": prompt_id,
                "buttonify_prompt_truncated": prompt_truncated,
            }
        )

    def _action_buttonify_extract_options(self, request: Any) -> WorkflowActionResult:
        screen_text = request.data.get("screen_text")
        if not isinstance(screen_text, str):
            screen_text = ""
        user_prompt = request.data.get("user_prompt")
        if not isinstance(user_prompt, str):
            user_prompt = ""

        buttonify_preflight_enabled = bool(
            request.data.get("buttonify_preflight_enabled")
        )
        buttonify_prompt_text = request.data.get("buttonify_prompt_text")
        if not isinstance(buttonify_prompt_text, str):
            buttonify_prompt_text = ""

        buttonify_prompt_id = request.data.get("buttonify_prompt_id")
        if not isinstance(buttonify_prompt_id, str):
            buttonify_prompt_id = None
        buttonify_prompt_truncated = bool(request.data.get("buttonify_prompt_truncated"))

        buttonify_model_used = request.data.get("default_model") or request.environment.model
        buttonify_options: list[str] = []
        buttonify_source = "none"
        buttonify_error_class: str | None = None
        buttonify_model_attempted = False
        buttonify_suppression_reason = request.data.get("buttonify_suppression_reason")
        if not isinstance(buttonify_suppression_reason, str):
            buttonify_suppression_reason = None
        buttonify_preflight_rejection_reason: str | None = None

        if buttonify_preflight_enabled:
            preflight_candidates = extract_buttonify_options_heuristic(screen_text)
            preflight_options, buttonify_preflight_rejection_reason = (
                select_buttonify_preflight_options(preflight_candidates)
            )
            if preflight_options:
                buttonify_options = preflight_options
                buttonify_source = "heuristic_preflight"

        if not buttonify_options:
            buttonify_model_attempted = True
            buttonify_response = None

            policy_state = request.data.get("policy_state")
            record_llm_call = request.data.get("record_llm_call")
            aux_llm_calls = request.data.get("aux_llm_calls")
            registry_snapshot = request.data.get("registry_snapshot")
            user_concept_id = request.data.get("user_concept_id")
            org_concept_id = request.data.get("org_concept_id")
            llm_calls_log = request.data.get("llm_calls_log")

            if (
                isinstance(policy_state, _WorkflowModelPolicyState)
                and callable(record_llm_call)
                and isinstance(aux_llm_calls, list)
                and isinstance(llm_calls_log, list)
            ):
                try:
                    buttonify_response, buttonify_model_used, _ = (
                        self._run_llm_with_fallbacks(
                            stage="buttonify",
                            prompt=buttonify_prompt_text,
                            context=[],
                            default_client=request.environment.llm_client,
                            default_model=buttonify_model_used,
                            policy_state=policy_state,
                            registry_snapshot=(
                                registry_snapshot
                                if isinstance(registry_snapshot, Mapping)
                                else None
                            ),
                            user_concept_id=(
                                user_concept_id if isinstance(user_concept_id, str) else None
                            ),
                            org_concept_id=(
                                org_concept_id if isinstance(org_concept_id, str) else None
                            ),
                            llm_calls_log=llm_calls_log,
                            aux_log=aux_llm_calls,
                            record_llm_call=cast(Callable[..., Any], record_llm_call),
                        )
                    )
                except Exception as exc:
                    buttonify_error_class = type(exc).__name__
                    buttonify_response = None

            if buttonify_response is None:
                llm_start = time.perf_counter()
                try:
                    buttonify_response = request.environment.llm_client.generate(
                        prompt=buttonify_prompt_text,
                        context=[],
                        model=buttonify_model_used,
                    )
                    if callable(record_llm_call):
                        cast(Callable[..., Any], record_llm_call)(
                            call_type="llm.generate",
                            model_name=buttonify_model_used,
                            duration_ms=(time.perf_counter() - llm_start) * 1000.0,
                            usage=None,
                            note="Buttonify quick-reply extraction (workflow fallback).",
                            stage="buttonify",
                        )
                except Exception as exc:
                    buttonify_error_class = type(exc).__name__
                    buttonify_response = None

            buttonify_options = parse_buttonify_options_json(buttonify_response)
            buttonify_source = "llm" if buttonify_options else "none"

            if not buttonify_options:
                heuristic_options = sanitise_buttonify_heuristic_options(
                    extract_buttonify_options_heuristic(screen_text)
                )
                if heuristic_options:
                    buttonify_options = heuristic_options
                    buttonify_source = "heuristic_fallback"

        # Defensively re-normalise options to preserve deterministic UI constraints.
        buttonify_options = dedupe_buttonify_options(buttonify_options)

        if buttonify_source in {"heuristic_preflight", "llm"}:
            buttonify_status = "success"
            buttonify_suppression_reason = None
        elif buttonify_source == "heuristic_fallback":
            buttonify_status = "fallback_success"
            buttonify_suppression_reason = "fallback_heuristic_used"
        else:
            buttonify_status = "no_op"
            if not buttonify_suppression_reason:
                if buttonify_error_class:
                    buttonify_suppression_reason = "model_error"
                elif buttonify_preflight_rejection_reason:
                    buttonify_suppression_reason = buttonify_preflight_rejection_reason
                else:
                    buttonify_suppression_reason = "no_candidates"

        contract = self._build_output_transformation_contract(
            transform_name="buttonify",
            transform_version="v1",
            workflow_id=CHAT_BUTTONIFY_WORKFLOW_ID,
            stage_id="extract_options",
            input_payload={
                "screen_text_chars": len(screen_text),
                "user_prompt_chars": len(user_prompt),
                "heuristic_preflight_enabled": buttonify_preflight_enabled,
            },
            output_payload={
                "options": list(buttonify_options),
                "source": buttonify_source,
                "prompt_id": buttonify_prompt_id,
                "prompt_truncated": buttonify_prompt_truncated,
            },
            diagnostics={
                "status": buttonify_status,
                "suppression_reason": buttonify_suppression_reason,
                "preflight_rejection_reason": buttonify_preflight_rejection_reason,
                "error_class": buttonify_error_class,
                "model": buttonify_model_used if buttonify_model_attempted else None,
            },
        )

        return WorkflowActionResult(
            outputs={
                "buttonify_status": buttonify_status,
                "buttonify_options": buttonify_options,
                "buttonify_source": buttonify_source,
                "buttonify_prompt_id": buttonify_prompt_id,
                "buttonify_prompt_truncated": buttonify_prompt_truncated,
                "buttonify_model_used": buttonify_model_used,
                "buttonify_model_attempted": buttonify_model_attempted,
                "buttonify_error_class": buttonify_error_class,
                "buttonify_suppression_reason": buttonify_suppression_reason,
                "buttonify_preflight_rejection_reason": (
                    buttonify_preflight_rejection_reason
                ),
                "output_transformation_contract": contract,
            }
        )

    # ------------------------------------------------------------------
    # Todo-refresh workflow handlers (JVNAUTOSCI-922 Phase 2.3)
    #
    # Five handlers that replace the ``_noop_action`` stubs registered for
    # the ``#V#todo_refresh_workflow`` state machine:
    #   check_cache -> maybe_fetch_gmail -> extract_tasks -> prioritise
    #   -> summarise -> completed
    #
    # Key data-dict fields consumed/produced:
    #   user_namespace, gmail_profile (inputs from caller),
    #   todo_refresh_needed (flag: cache stale?), raw_gmail_messages,
    #   extracted_tasks (list[dict]), prioritised_tasks (list[dict]),
    #   persisted_tasks (list[dict]), todo_summary (str).
    # ------------------------------------------------------------------

    # Configurable via env var; how many seconds before tasks are
    # considered stale and a refresh from Gmail is warranted.
    _TODO_CACHE_TTL_SECONDS: int = int(
        os.environ.get("VON_TODO_CACHE_TTL_SECONDS", "3600")
    )

    def _action_todo_refresh_check_cache(self, request: Any) -> WorkflowActionResult:
        """Check whether the user's task list is fresh enough to skip Gmail.

        Looks at the most recently created ``#V#task_specification`` for the
        current user.  If it was created less than ``_TODO_CACHE_TTL_SECONDS``
        ago, sets ``todo_refresh_needed=False`` and the workflow short-circuits
        to *completed*.
        """
        from ...services.task_management_service import get_tasks_for_user

        data = request.data
        user_ns = data.get("user_namespace") or getattr(
            request.environment, "user_namespace", None
        )

        if not user_ns:
            # No user context - nothing to cache-check; proceed with refresh.
            self._logger.debug("[todo_refresh] No user namespace; skipping cache check")
            return WorkflowActionResult(
                outputs={"todo_refresh_needed": True, "result": True}
            )

        try:
            existing_tasks = get_tasks_for_user(user_ns)
        except Exception as exc:
            self._logger.warning(
                "[todo_refresh] Cache check failed: %s - proceeding with refresh",
                exc,
            )
            return WorkflowActionResult(
                outputs={"todo_refresh_needed": True, "result": True}
            )

        if not existing_tasks:
            return WorkflowActionResult(
                outputs={"todo_refresh_needed": True, "result": True}
            )

        # Find most recent updated_at / created_at.
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        newest_ts: datetime | None = None
        for task in existing_tasks:
            for key in ("updated_at", "created_at"):
                raw = task.get(key)
                if raw is None:
                    continue
                if isinstance(raw, str):
                    try:
                        raw = datetime.fromisoformat(raw)
                    except (ValueError, TypeError):
                        continue
                if isinstance(raw, datetime):
                    if raw.tzinfo is None:
                        raw = raw.replace(tzinfo=timezone.utc)
                    if newest_ts is None or raw > newest_ts:
                        newest_ts = raw

        if newest_ts is None:
            return WorkflowActionResult(
                outputs={"todo_refresh_needed": True, "result": True}
            )

        age_seconds = (now - newest_ts).total_seconds()
        stale = age_seconds > self._TODO_CACHE_TTL_SECONDS
        self._logger.info(
            "[todo_refresh] Cache age %.0fs, TTL %ds -> %s",
            age_seconds,
            self._TODO_CACHE_TTL_SECONDS,
            "stale" if stale else "fresh",
        )
        return WorkflowActionResult(outputs={"todo_refresh_needed": stale, "result": stale})

    def _action_todo_refresh_fetch_gmail(self, request: Any) -> WorkflowActionResult:
        """Fetch recent Gmail messages via the MCP gateway.

        Retrieves up to 10 recent messages using ``gmail_list_messages`` and
        then fetches full bodies for messages that look actionable (contain
        common task/todo keywords in the snippet).
        """
        data = request.data
        gateway = request.environment.gateway
        gmail_profile = data.get("gmail_profile") or getattr(
            request.environment, "default_gmail_profile", None
        )

        if not gateway:
            self._logger.warning("[todo_refresh] No gateway available")
            return WorkflowActionResult(
                outputs={"raw_gmail_messages": [], "gmail_fetch_ok": False}
            )

        if not gmail_profile:
            self._logger.warning(
                "[todo_refresh] No gmail_profile configured; skipping fetch"
            )
            return WorkflowActionResult(
                outputs={"raw_gmail_messages": [], "gmail_fetch_ok": False}
            )

        # List recent messages.
        try:
            list_result = gateway.invoke(
                "gmail_list_messages",
                {
                    "profile": gmail_profile,
                    "max_results": 10,
                    "query": "newer_than:3d",
                },
            )
            messages_list = (list_result.payload or {}).get("messages", [])
        except Exception as exc:
            self._logger.warning("[todo_refresh] gmail_list_messages failed: %s", exc)
            return WorkflowActionResult(
                outputs={"raw_gmail_messages": [], "gmail_fetch_ok": False}
            )

        if not messages_list:
            self._logger.info("[todo_refresh] No recent Gmail messages found")
            return WorkflowActionResult(
                outputs={"raw_gmail_messages": [], "gmail_fetch_ok": True}
            )

        full_messages: list[dict[str, Any]] = []
        for msg_summary in messages_list[:10]:
            msg_id = msg_summary.get("id")
            if not msg_id:
                continue
            try:
                detail_result = gateway.invoke(
                    "gmail_get_message",
                    {"profile": gmail_profile, "message_id": msg_id},
                )
                if detail_result.payload:
                    full_messages.append(detail_result.payload)
            except Exception as exc:
                self._logger.debug(
                    "[todo_refresh] gmail_get_message(%s) failed: %s", msg_id, exc
                )

        self._logger.info(
            "[todo_refresh] Fetched %d/%d Gmail messages",
            len(full_messages),
            len(messages_list),
        )
        return WorkflowActionResult(
            outputs={
                "raw_gmail_messages": full_messages,
                "gmail_fetch_ok": True,
            }
        )

    def _action_todo_refresh_extract_tasks(self, request: Any) -> WorkflowActionResult:
        """Use the LLM to extract actionable tasks from Gmail message bodies.

        Reads ``raw_gmail_messages`` from the workflow context, builds a prompt
        asking the model to identify tasks, and parses the structured JSON
        response.  Outputs ``extracted_tasks`` - a list of dicts with keys
        ``title``, ``description``, ``source_email_id``.
        """
        data = request.data
        raw_messages: list[dict[str, Any]] = data.get("raw_gmail_messages") or []

        if not raw_messages:
            self._logger.info("[todo_refresh] No messages to extract tasks from")
            return WorkflowActionResult(outputs={"extracted_tasks": []})

        # Build a digest of email contents for the LLM.
        digest_parts: list[str] = []
        for idx, msg in enumerate(raw_messages, 1):
            subject = msg.get("subject") or msg.get("headers", {}).get(
                "Subject", "(no subject)"
            )
            sender = msg.get("from") or msg.get("headers", {}).get("From", "unknown")
            body = msg.get("body") or msg.get("snippet") or ""
            # Truncate very long bodies.
            if len(body) > 2000:
                body = body[:2000] + "..."
            digest_parts.append(
                f"### Email {idx}\nFrom: {sender}\nSubject: {subject}\n\n{body}\n"
            )

        email_digest = "\n---\n".join(digest_parts)

        extraction_prompt = (
            "You are an expert personal assistant.  Analyse the following email "
            "messages and extract actionable to-do items.  For each task, provide:\n"
            "- title: a short (~10 word) task title\n"
            "- description: 1-2 sentence description of what needs to be done\n"
            "- source_email_index: which email number (1-based) it came from\n\n"
            "Return your answer as a JSON array of objects.  If there are no "
            "actionable items, return an empty array [].\n\n"
            "EMAILS:\n" + email_digest
        )

        llm_client = request.environment.llm_client
        model = request.environment.model
        if not llm_client:
            self._logger.warning("[todo_refresh] No LLM client for extraction")
            return WorkflowActionResult(outputs={"extracted_tasks": []})

        try:
            llm_response = llm_client.generate(
                prompt=extraction_prompt,
                context=[
                    {
                        "role": "system",
                        "content": (
                            "You extract actionable tasks from emails.  "
                            "Reply ONLY with a JSON array.  No markdown fences."
                        ),
                    },
                ],
                model=model,
            )
        except Exception as exc:
            self._logger.warning("[todo_refresh] LLM extraction failed: %s", exc)
            return WorkflowActionResult(outputs={"extracted_tasks": []})

        # Parse the JSON array from the response.
        extracted: list[dict[str, Any]] = []
        try:
            import json as _json

            text = (
                llm_response.strip()
                if isinstance(llm_response, str)
                else str(llm_response).strip()
            )
            # Strip markdown code fences if present.
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(
                    lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
                )
            parsed = _json.loads(text)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("title"):
                        extracted.append(
                            {
                                "title": str(item["title"]),
                                "description": str(item.get("description", "")),
                                "source_email_index": item.get("source_email_index"),
                            }
                        )
        except Exception as exc:
            self._logger.warning(
                "[todo_refresh] Failed to parse extracted tasks: %s", exc
            )

        # Log to aux_llm_calls for observability.
        aux_log = data.get("aux_llm_calls")
        if isinstance(aux_log, list):
            aux_log.append(
                {
                    "type": "todo_refresh.extract_tasks",
                    "model": model,
                    "email_count": len(raw_messages),
                    "extracted_count": len(extracted),
                }
            )

        self._logger.info(
            "[todo_refresh] Extracted %d tasks from %d emails",
            len(extracted),
            len(raw_messages),
        )
        return WorkflowActionResult(outputs={"extracted_tasks": extracted})

    def _action_todo_refresh_prioritise(self, request: Any) -> WorkflowActionResult:
        """Assign priority (low/medium/high/critical) to extracted tasks.

        If there are ≤3 tasks, uses a simple heuristic (all medium).
        For larger batches, calls the LLM to assign priorities
        contextually.
        """
        data = request.data
        extracted: list[dict[str, Any]] = data.get("extracted_tasks") or []

        if not extracted:
            return WorkflowActionResult(outputs={"prioritised_tasks": []})

        # Simple heuristic for small batches - avoids an LLM call.
        if len(extracted) <= 3:
            for task in extracted:
                task.setdefault("priority", "medium")
            return WorkflowActionResult(outputs={"prioritised_tasks": extracted})

        # Use LLM for larger batches.
        import json as _json

        task_summaries = _json.dumps(
            [
                {
                    "index": i,
                    "title": t["title"],
                    "description": t.get("description", ""),
                }
                for i, t in enumerate(extracted)
            ],
            indent=2,
        )
        prioritise_prompt = (
            "Given these tasks, assign a priority to each: low, medium, high, "
            "or critical.  Consider urgency, deadlines, and importance.\n\n"
            "Tasks:\n" + task_summaries + "\n\n"
            "Return a JSON array of objects with keys: index, priority."
        )

        llm_client = request.environment.llm_client
        model = request.environment.model

        try:
            llm_response = llm_client.generate(
                prompt=prioritise_prompt,
                context=[
                    {
                        "role": "system",
                        "content": "You prioritise tasks.  Reply ONLY with a JSON array.",
                    },
                ],
                model=model,
            )
            text = (
                llm_response.strip()
                if isinstance(llm_response, str)
                else str(llm_response).strip()
            )
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(
                    lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
                )
            parsed = _json.loads(text)
            if isinstance(parsed, list):
                priority_map = {}
                for item in parsed:
                    if isinstance(item, dict):
                        idx = item.get("index")
                        prio = item.get("priority", "medium")
                        if isinstance(idx, int) and prio in (
                            "low",
                            "medium",
                            "high",
                            "critical",
                        ):
                            priority_map[idx] = prio
                for i, task in enumerate(extracted):
                    task["priority"] = priority_map.get(i, "medium")
        except Exception as exc:
            self._logger.warning(
                "[todo_refresh] LLM prioritisation failed: %s - defaulting to medium",
                exc,
            )
            for task in extracted:
                task.setdefault("priority", "medium")

        return WorkflowActionResult(outputs={"prioritised_tasks": extracted})

    def _action_todo_refresh_summarise(self, request: Any) -> WorkflowActionResult:
        """Persist prioritised tasks via the task management service and
        produce a human-readable summary.

        Creates each task as a ``#V#task_specification`` concept with the
        appropriate priority and assignee.  Skips tasks whose titles match
        an existing pending task for this user (deduplication).
        """
        from ...services.task_management_service import (
            create_task,
            get_tasks_for_user,
        )

        data = request.data
        prioritised: list[dict[str, Any]] = data.get("prioritised_tasks") or []
        user_ns = data.get("user_namespace") or getattr(
            request.environment, "user_namespace", None
        )

        if not prioritised:
            return WorkflowActionResult(
                outputs={
                    "persisted_tasks": [],
                    "todo_summary": "No new tasks found from recent emails.",
                }
            )

        # Deduplicate against existing pending tasks.
        existing_titles: set[str] = set()
        if user_ns:
            try:
                existing = get_tasks_for_user(user_ns, status_filter="pending")
                existing_titles = {
                    (t.get("title") or "").strip().lower() for t in existing
                }
            except Exception:
                pass

        persisted: list[dict[str, Any]] = []
        skipped = 0
        for task in prioritised:
            title = (task.get("title") or "").strip()
            if not title:
                continue
            if title.lower() in existing_titles:
                skipped += 1
                continue

            try:
                result = create_task(
                    title=title,
                    description=task.get("description") or title,
                    priority=task.get("priority", "medium"),
                    assignee_concept_id=user_ns,
                    created_by_concept_id=user_ns,
                )
                persisted.append(result)
                existing_titles.add(title.lower())
            except Exception as exc:
                self._logger.warning(
                    "[todo_refresh] Failed to create task '%s': %s",
                    title,
                    exc,
                )

        # Build human-readable summary.
        summary_lines = [f"Refreshed to-do list: {len(persisted)} new task(s) created"]
        if skipped:
            summary_lines[0] += f", {skipped} duplicate(s) skipped"
        summary_lines[0] += "."
        for t in persisted:
            prio = t.get("priority", "medium")
            summary_lines.append(f"  • [{prio.upper()}] {t.get('title', 'Untitled')}")

        summary = "\n".join(summary_lines)
        self._logger.info(
            "[todo_refresh] Persisted %d tasks, skipped %d duplicates",
            len(persisted),
            skipped,
        )

        return WorkflowActionResult(
            outputs={
                "persisted_tasks": persisted,
                "todo_summary": summary,
            }
        )

    # ------------------------------------------------------------------
    # Tool-calling workflow handlers (JVNAUTOSCI-922 Phase 2)
    #
    # These four handlers encapsulate the procedural tool-calling logic
    # that was previously inline in ``run()``.  They operate on a shared
    # ``data`` dict (the workflow context) and return outputs that drive
    # state transitions in ``#V#tool_calling_workflow``.
    #
    # Key data-dict fields consumed/produced:
    #   prompt, augmented_context (mutable list), policy_state,
    #   registry_snapshot, user/org_concept_id, model_for_stage (callable),
    #   record_llm_call (callable), emit_progress / emit_phase_transition /
    #   check_cancellation (callables), aux_llm_calls / llm_calls (mutable
    #   lists), invocations / tool_messages (mutable lists),
    #   tool_calls, tool_calls_present, iteration_count, final_response,
    #   orchestrator_result (pre-built OrchestratorResult for error exits).
    # ------------------------------------------------------------------

    def _action_tool_calling_plan(self, request: Any) -> WorkflowActionResult:
        """Phase 1 of tool calling: initial LLM call + missing-tool-call recovery.

        Determines structured vs legacy path, makes the LLM call, interprets
        the response, and runs the missing-tool-call recovery workflow if no
        valid tool calls are found.

        Outputs:
            tool_calls_present (bool): True if tool calls were extracted.
            direct_response (bool): True if no tools needed (plain text).
            response (str): The raw LLM response text.
            tool_calls (list | None): Extracted tool-call dicts.
            use_structured (bool): Whether structured calling was used.
            orchestrator_result (OrchestratorResult | None): Pre-built error
                result if a parse error terminates the flow early.
        """
        data = request.data
        env = request.environment
        llm_client = env.llm_client
        prompt = data["prompt"]
        augmented_context = data["augmented_context"]
        policy_state = data["policy_state"]
        registry_snapshot = data.get("registry_snapshot")
        user_concept_id = data.get("user_concept_id")
        org_concept_id = data.get("org_concept_id")
        model_for_stage = data["model_for_stage"]
        record_llm_call = data["record_llm_call"]
        emit_progress_raw = data.get("emit_progress")
        emit_progress_cb: Callable[[Mapping[str, Any]], None] | None = (
            cast(Callable[[Mapping[str, Any]], None], emit_progress_raw)
            if callable(emit_progress_raw)
            else None
        )
        emit_phase_transition = data.get("emit_phase_transition")
        aux_llm_calls = data["aux_llm_calls"]
        llm_calls = data["llm_calls"]
        missing_tool_call_retry_attempts = self._coerce_non_negative_int(
            data.get("missing_tool_call_retry_attempts"),
            default=0,
            max_value=20,
        )
        missing_tool_call_retry_budget = self._coerce_non_negative_int(
            data.get("missing_tool_call_retry_budget"),
            default=self._max_missing_tool_call_retries_per_turn,
            max_value=20,
        )
        missing_tool_call_retry_suppressed = bool(
            data.get("missing_tool_call_retry_suppressed")
        )

        # Emit planning phase.
        if callable(emit_phase_transition):
            emit_phase_transition(
                self.PHASE_TOOL_PLAN,
            )

        # Determine structured vs legacy calling.
        use_structured = (
            hasattr(llm_client, "generate_with_tools")
            and hasattr(llm_client, "_should_use_structured_calling")
            and llm_client._should_use_structured_calling()
        )
        if policy_state.enabled and policy_state.policy:
            use_structured = True

        response = ""
        tool_calls = None
        has_valid_tool_call = False
        tool_call_parse_error: ToolCallParsingError | None = None
        interpretation = None

        if use_structured:
            self._logger.debug("[mcp_orchestrator] Using structured tool calling path")
            try:
                tool_call_model = model_for_stage("tool_call")
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
                    record_llm_call=record_llm_call,
                    emit_progress=emit_progress_cb,
                )
                if llm_response.tool_calls:
                    response = llm_response.text_response or ""
                    tool_calls = [
                        {
                            self._ACTION_FIELD: self._CALL_ACTION,
                            self._TOOL_FIELD: tc.tool_name,
                            self._PAYLOAD_FIELD: tc.payload,
                            "_call_id": tc.call_id,
                        }
                        for tc in llm_response.tool_calls
                    ]
                    has_valid_tool_call = True
                else:
                    response = llm_response.text_response
                    tool_calls = None
                    has_valid_tool_call = False
            except Exception as exc:
                self._logger.warning(
                    "[mcp_orchestrator] Structured calling failed, falling back to legacy: %s",
                    exc,
                )
                use_structured = False

        if not use_structured:
            tool_call_model = model_for_stage("tool_call")
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
                record_llm_call=record_llm_call,
                emit_progress=emit_progress_cb,
            )
            tool_calls = None
            has_valid_tool_call = False

        # Interpret response (legacy path only).
        if not use_structured:
            interpretation = self._interpret_model_turn(response)
            response = interpretation.response_text
            tool_calls = interpretation.tool_calls
            tool_call_parse_error = interpretation.tool_call_parse_error
            has_valid_tool_call = bool(tool_calls)

        method_catalogue_for_requirements = data.get("method_catalogue")
        if not isinstance(method_catalogue_for_requirements, Mapping):
            try:
                method_catalogue_for_requirements = self._gateway.describe_methods()
            except Exception:
                method_catalogue_for_requirements = None

        prompt_requirement_state = self._derive_prompt_tool_requirements(
            prompt,
            method_catalogue=(
                method_catalogue_for_requirements
                if isinstance(method_catalogue_for_requirements, Mapping)
                else None
            ),
        )
        required_prompt_tools = list(
            cast(list[str], prompt_requirement_state.get("required_tools") or [])
        )
        required_prompt_fetch_concept_ids = list(
            cast(
                list[str],
                prompt_requirement_state.get("required_fetch_concept_ids") or [],
            )
        )
        required_prompt_create_type_name_raw = prompt_requirement_state.get(
            "required_create_type_name"
        )
        required_prompt_create_type_name = (
            str(required_prompt_create_type_name_raw).strip()
            if isinstance(required_prompt_create_type_name_raw, str)
            else None
        )
        data["required_prompt_tools"] = list(required_prompt_tools)
        data["required_prompt_fetch_concept_ids"] = list(
            required_prompt_fetch_concept_ids
        )
        data["required_prompt_create_type_name"] = required_prompt_create_type_name

        # Missing-tool-call recovery.
        if not has_valid_tool_call:
            missing_prompt_tools, missing_prompt_fetch_concept_ids = (
                self._derive_missing_prompt_requirements(
                    required_tools=required_prompt_tools,
                    required_fetch_concept_ids=required_prompt_fetch_concept_ids,
                    tool_invocations=(),
                )
            )
            data["missing_prompt_fetch_concept_ids"] = list(
                missing_prompt_fetch_concept_ids
            )
            missing_retry_reason = self._build_missing_prompt_retry_reason(
                missing_tools=missing_prompt_tools,
                missing_fetch_concept_ids=missing_prompt_fetch_concept_ids,
            )
            data["missing_prompt_tools"] = list(missing_prompt_tools)
            if missing_retry_reason:
                data["missing_tool_call_retry_reason_override"] = missing_retry_reason
            else:
                data.pop("missing_tool_call_retry_reason_override", None)

            recovery_data = self._run_missing_tool_call_recovery_workflow(
                request=request,
                environment=env,
                data=data,
                response_text=(response if isinstance(response, str) else str(response)),
                interpretation=interpretation,
                use_structured=use_structured,
                tool_call_parse_error=tool_call_parse_error,
                tool_calls=tool_calls,
                default_model=tool_call_model,
            )
            if recovery_data is not None:
                response = recovery_data.get("response_text", response)
                interpretation = recovery_data.get("interpretation", interpretation)
                tool_calls = recovery_data.get("tool_calls", tool_calls)
                tool_call_parse_error = recovery_data.get(
                    "tool_call_parse_error", tool_call_parse_error
                )
                missing_tool_call_retry_attempts = self._coerce_non_negative_int(
                    recovery_data.get(
                        "missing_tool_call_retry_attempts",
                        missing_tool_call_retry_attempts,
                    ),
                    default=missing_tool_call_retry_attempts,
                    max_value=20,
                )
                missing_tool_call_retry_budget = self._coerce_non_negative_int(
                    recovery_data.get(
                        "missing_tool_call_retry_budget",
                        missing_tool_call_retry_budget,
                    ),
                    default=missing_tool_call_retry_budget,
                    max_value=20,
                )
                missing_tool_call_retry_suppressed = bool(
                    recovery_data.get("missing_tool_call_retry_suppressed")
                )
                has_valid_tool_call = bool(tool_calls)

        data["missing_tool_call_retry_attempts"] = missing_tool_call_retry_attempts
        data["missing_tool_call_retry_budget"] = missing_tool_call_retry_budget
        data["missing_tool_call_retry_suppressed"] = (
            missing_tool_call_retry_suppressed
        )
        missing_tool_call_retry_remaining = max(
            0, missing_tool_call_retry_budget - missing_tool_call_retry_attempts
        )

        # Determine next state.
        if has_valid_tool_call:
            return WorkflowActionResult(
                outputs={
                    "tool_calls_present": True,
                    "direct_response": False,
                    "response": response,
                    "tool_calls": tool_calls,
                    "use_structured": use_structured,
                    "tool_call_model": tool_call_model,
                    "missing_tool_call_retry_attempts": missing_tool_call_retry_attempts,
                    "missing_tool_call_retry_budget": missing_tool_call_retry_budget,
                    "missing_tool_call_retry_remaining": missing_tool_call_retry_remaining,
                    "missing_tool_call_retry_suppressed": missing_tool_call_retry_suppressed,
                    "result": True,
                }
            )

        # No tool calls — direct response or error.
        if tool_call_parse_error is not None:
            build_error = data.get("build_parse_error_result")
            if callable(build_error):
                error_result = build_error(tool_call_parse_error)
                return WorkflowActionResult(
                    outputs={
                        "tool_calls_present": False,
                        "direct_response": True,
                        "orchestrator_result": error_result,
                        "missing_tool_call_retry_attempts": missing_tool_call_retry_attempts,
                        "missing_tool_call_retry_budget": missing_tool_call_retry_budget,
                        "missing_tool_call_retry_remaining": missing_tool_call_retry_remaining,
                        "missing_tool_call_retry_suppressed": missing_tool_call_retry_suppressed,
                        "result": False,
                    }
                )

        return WorkflowActionResult(
            outputs={
                "tool_calls_present": False,
                "direct_response": True,
                "final_response": (
                    response if isinstance(response, str) else str(response)
                ),
                "missing_tool_call_retry_attempts": missing_tool_call_retry_attempts,
                "missing_tool_call_retry_budget": missing_tool_call_retry_budget,
                "missing_tool_call_retry_remaining": missing_tool_call_retry_remaining,
                "missing_tool_call_retry_suppressed": missing_tool_call_retry_suppressed,
                "result": False,
            }
        )

    def _action_tool_calling_validate(self, request: Any) -> WorkflowActionResult:
        """Preflight validation, coercion, and repair of extracted tool calls.

        Runs ``_preflight_tool_calls`` and on errors attempts
        ``_attempt_tool_call_repair``.  If validation still fails, produces
        a pre-built ``orchestrator_result`` that terminates the workflow.

        Outputs:
            tool_calls_validated (bool): True if tool calls passed validation.
            tool_calls (list): Possibly repaired tool calls.
            orchestrator_result (OrchestratorResult | None): Error result.
        """
        data = request.data
        env = request.environment
        llm_client = env.llm_client
        tool_calls = data.get("tool_calls")
        if not tool_calls:
            return WorkflowActionResult(
                outputs={
                    "tool_calls_validated": False,
                    "tool_calls_present": False,
                    "result": False,
                }
            )

        prompt = data.get("prompt", "")
        augmented_context = data.get("augmented_context", [])
        policy_state = data["policy_state"]
        registry_snapshot = data.get("registry_snapshot")
        user_concept_id = data.get("user_concept_id")
        org_concept_id = data.get("org_concept_id")
        model_for_stage = data["model_for_stage"]
        record_llm_call = data["record_llm_call"]
        aux_llm_calls = data["aux_llm_calls"]
        llm_calls = data["llm_calls"]
        gmail_profile = data.get("gmail_profile") or env.default_gmail_profile
        conversation_session_id = data.get("conversation_session_id")

        # Build method catalogue on first validation pass.
        if "method_catalogue" not in data:
            method_catalogue = self._gateway.describe_methods()
            tool_categories: dict[str, str] = {}
            for name, meta in method_catalogue.items():
                if not isinstance(meta, Mapping):
                    continue
                category = meta.get("category")
                if isinstance(category, str):
                    tool_categories[name] = category
            data["method_catalogue"] = method_catalogue
            data["tool_categories"] = tool_categories
        method_catalogue = data["method_catalogue"]

        preflight = self._preflight_tool_calls(
            cast(list, tool_calls),
            method_catalogue,
            user_namespace=env.user_namespace,
            selected_gmail_profile=gmail_profile,
            conversation_session_id=conversation_session_id,
            turn_id=data.get("turn_id"),
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
                tool_call_model = data.get("tool_call_model") or model_for_stage(
                    "tool_call"
                )
                repaired_calls = self._attempt_tool_call_repair(
                    current_response=(
                        data.get("response", "")
                        if isinstance(data.get("response"), str)
                        else str(data.get("response", ""))
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
                    record_llm_call=record_llm_call,
                )
            if repaired_calls:
                preflight = self._preflight_tool_calls(
                    repaired_calls,
                    method_catalogue,
                    user_namespace=env.user_namespace,
                    selected_gmail_profile=gmail_profile,
                    conversation_session_id=conversation_session_id,
                    turn_id=data.get("turn_id"),
                )
                if not preflight.errors:
                    tool_calls = repaired_calls

        if preflight.errors:
            invocations = data.get("invocations", [])
            tool_messages = data.get("tool_messages", [])
            build_error = data.get("build_validation_error_result")
            if callable(build_error):
                error_result = build_error(
                    preflight.errors,
                    preflight.warnings,
                    preflight.tool_unavailable,
                    raw_tool_call=(
                        data.get("response", "")
                        if isinstance(data.get("response"), str)
                        else str(data.get("response", ""))
                    ),
                    invocations_override=tuple(invocations),
                    tool_messages_override=tuple(tool_messages),
                )
                return WorkflowActionResult(
                    outputs={
                        "tool_calls_validated": False,
                        "orchestrator_result": error_result,
                        "result": False,
                    }
                )
            return WorkflowActionResult(
                outputs={"tool_calls_validated": False, "result": False},
                status="failed",
                error="validation_failed: " + "; ".join(preflight.errors),
            )

        return WorkflowActionResult(
            outputs={
                "tool_calls_validated": True,
                "tool_calls": tool_calls,
                "result": True,
            }
        )

    def _action_tool_calling_execute(self, request: Any) -> WorkflowActionResult:
        """Execute a batch of tool calls against the MCP gateway.

        Handles batch-capping, write-policy enforcement, payload defaults,
        gateway invocation, and progress emission.  Mutates
        ``augmented_context``, ``invocations``, and ``tool_messages`` in
        the shared data dict.

        Outputs:
            tool_execution_complete (bool): Always True on success.
            iteration_count (int): Total tools executed so far.
        """
        data = request.data
        env = request.environment
        llm_client = env.llm_client
        prompt = data.get("prompt", "")
        augmented_context = data["augmented_context"]
        tool_calls = data.get("tool_calls") or []
        method_catalogue = data.get("method_catalogue", {})
        tool_categories = data.get("tool_categories", {})
        invocations = data.setdefault("invocations", [])
        tool_messages = data.setdefault("tool_messages", [])
        iteration_count = data.get("iteration_count", 0)
        allowed_write_tools: set = data.get("allowed_write_tools", set())
        recent_user_prompts = data.get("recent_user_prompts", [])
        gmail_profile = data.get("gmail_profile") or env.default_gmail_profile
        conversation_session_id = data.get("conversation_session_id")
        emit_progress = data.get("emit_progress")
        check_cancellation = data.get("check_cancellation")
        max_tool_invocations = env.max_tool_invocations or 8
        batch_cap = max(1, int(getattr(self, "_tool_batch_cap", 4)))

        # Emit tool_execute phase.
        emit_phase_transition = data.get("emit_phase_transition")
        if callable(emit_phase_transition) and iteration_count == 0:
            emit_phase_transition(
                self.PHASE_TOOL_EXECUTE,
                extra={
                    "tool_calls_cap": int(max_tool_invocations),
                    "tool_batch_cap": int(batch_cap),
                },
            )

        # Enforce invocation limit + batch cap.
        remaining = max_tool_invocations - iteration_count
        if remaining <= 0:
            return WorkflowActionResult(
                outputs={
                    "tool_execution_complete": True,
                    "iteration_count": iteration_count,
                }
            )
        allowed_count = min(remaining, batch_cap)
        remaining_tool_calls = []
        if len(tool_calls) > allowed_count:
            remaining_tool_calls = list(tool_calls[allowed_count:])
            tool_calls = list(tool_calls[:allowed_count])

        current_batch_size = len(tool_calls)

        # Add assistant response to context (once per batch).
        response_text = data.get("response", "")
        if iteration_count == 0:
            augmented_context.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                ]
            )
        else:
            augmented_context.append(
                {
                    "role": "assistant",
                    "content": data.get("current_response", response_text),
                }
            )

        # Cancellation check.
        if callable(check_cancellation):
            try:
                check_cancellation()
            except Exception:
                return WorkflowActionResult(
                    outputs={
                        "tool_execution_complete": True,
                        "iteration_count": iteration_count,
                    },
                    status="failed",
                    error="cancelled",
                )

        write_policy_reason = data.get("write_policy_reason", "")
        turn_id = data.get("turn_id")

        for tool_request in tool_calls:
            iteration_count += 1
            tool_name = tool_request.get(self._TOOL_FIELD)
            payload = tool_request.get(self._PAYLOAD_FIELD) or {}
            call_id = tool_request.get("_call_id")

            if not isinstance(tool_name, str):
                continue
            if not isinstance(payload, dict):
                payload = dict(payload) if isinstance(payload, Mapping) else {}
            else:
                payload = dict(payload)
            tool_request[self._PAYLOAD_FIELD] = payload

            if callable(emit_progress):
                emit_progress(
                    {
                        "status": "tool_call_start",
                        "stage": self.PHASE_TOOL_EXECUTE,
                        "tool": tool_name,
                        "batch_size": current_batch_size,
                        "tool_calls_done": max(0, iteration_count - 1),
                        "tool_calls_cap": int(max_tool_invocations),
                        "tool_calls_remaining": max(
                            0, max_tool_invocations - iteration_count
                        ),
                        "call_id": call_id,
                    }
                )

            # Write-policy gate.
            tool_category = tool_categories.get(tool_name)
            write_interaction_metadata: dict[str, Any] | None = None
            if tool_category == "write":
                write_interaction_metadata = self._build_write_interaction_metadata(
                    tool_name=tool_name,
                    user_namespace=env.user_namespace,
                    conversation_session_id=conversation_session_id,
                    turn_id=turn_id if isinstance(turn_id, str) else None,
                )
            if tool_category == "write" and tool_name not in allowed_write_tools:
                allowed_write_tools, write_policy_reason = (
                    self._resolve_allowed_write_tools(
                        prompt=prompt,
                        requested_write_tools=[tool_name],
                        recent_user_prompts=recent_user_prompts,
                        llm_client=llm_client,
                        model=env.model,
                        user_namespace=env.user_namespace,
                        auxiliary_system_prompt=env.auxiliary_system_prompt,
                        trace=request.trace,
                        conversation_session_id=conversation_session_id,
                        turn_id=data.get("turn_id"),
                        aux_llm_calls=data.get("aux_llm_calls"),
                    )
                )

            if tool_category == "write" and tool_name not in allowed_write_tools:
                message = self._build_blocked_write_message(
                    tool_name=tool_name,
                    reason=write_policy_reason if isinstance(write_policy_reason, str) else None,
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
                if write_interaction_metadata:
                    blocked_record["knowledge_interaction"] = write_interaction_metadata
                if call_id:
                    blocked_record["call_id"] = call_id
                invocations.append(blocked_record)
                if callable(emit_progress):
                    emit_progress(
                        {
                            "status": "tool_blocked",
                            "tool": tool_name,
                            "batch_size": current_batch_size,
                            "tool_calls_done": iteration_count,
                            "tool_calls_cap": int(max_tool_invocations),
                            "tool_calls_remaining": max(
                                0, max_tool_invocations - iteration_count
                            ),
                            "call_id": call_id,
                            "error": message,
                        }
                    )
                augmented_context.append({"role": "tool", "content": tool_payload})
                tool_messages.append({"role": "tool", "content": tool_payload})
                continue

            high_impact_guard_reason: str | None = None
            if tool_category == "write":
                high_impact_guard_reason = self._evaluate_high_impact_write_guard(
                    tool_name=tool_name,
                    prompt=prompt if isinstance(prompt, str) else "",
                    recent_user_prompts=list(recent_user_prompts or []),
                    user_namespace=env.user_namespace,
                )

            if tool_category == "write" and high_impact_guard_reason:
                message = self._build_blocked_write_message(
                    tool_name=tool_name,
                    reason=high_impact_guard_reason,
                )
                tool_payload = self._format_tool_result(
                    tool_name, None, None, "error", message
                )
                blocked_record = {
                    "tool": tool_name,
                    "payload": dict(payload),
                    "error": message,
                    "blocked": True,
                    "write_policy_reason": high_impact_guard_reason,
                }
                if write_interaction_metadata:
                    blocked_record["knowledge_interaction"] = {
                        **write_interaction_metadata,
                        "guard_reason": high_impact_guard_reason,
                    }
                if call_id:
                    blocked_record["call_id"] = call_id
                invocations.append(blocked_record)
                if callable(emit_progress):
                    emit_progress(
                        {
                            "status": "tool_blocked",
                            "tool": tool_name,
                            "batch_size": current_batch_size,
                            "tool_calls_done": iteration_count,
                            "tool_calls_cap": int(max_tool_invocations),
                            "tool_calls_remaining": max(
                                0, max_tool_invocations - iteration_count
                            ),
                            "call_id": call_id,
                            "error": message,
                        }
                    )
                augmented_context.append({"role": "tool", "content": tool_payload})
                tool_messages.append({"role": "tool", "content": tool_payload})
                continue

            try:
                payload_before_invoke = dict(payload)
                schema = self._tool_schema_for_name(tool_name, method_catalogue)
                self._apply_payload_defaults(
                    tool_name,
                    payload,
                    schema=schema,
                    user_namespace=env.user_namespace,
                    selected_gmail_profile=gmail_profile,
                    conversation_session_id=conversation_session_id,
                    turn_id=data.get("turn_id"),
                )

                result = self._gateway.invoke(tool_name, payload)
                auto_retry_details: dict[str, Any] | None = None
                if tool_name == "add_relationship" and isinstance(
                    result.payload, Mapping
                ):
                    retry_payload, retry_context = (
                        self._retry_add_relationship_on_target_not_found(
                            payload=payload,
                            result_payload=cast(Mapping[str, Any], result.payload),
                        )
                    )
                    if retry_payload is not None and retry_context is not None:
                        auto_retry_details = dict(retry_context)
                        if callable(emit_progress):
                            emit_progress(
                                {
                                    "status": "tool_retry",
                                    "tool": tool_name,
                                    "batch_size": current_batch_size,
                                    "tool_calls_done": iteration_count,
                                    "tool_calls_cap": int(max_tool_invocations),
                                    "tool_calls_remaining": max(
                                        0, max_tool_invocations - iteration_count
                                    ),
                                    "call_id": call_id,
                                    "retry_reason": retry_context.get("reason"),
                                }
                            )
                        try:
                            retry_result = self._gateway.invoke(tool_name, retry_payload)
                        except Exception as retry_exc:
                            auto_retry_details["retry_error"] = str(retry_exc)
                        else:
                            result = retry_result
                            payload = retry_payload
                            tool_request[self._PAYLOAD_FIELD] = payload
                            auto_retry_details["retry_success"] = bool(
                                isinstance(result.payload, Mapping)
                                and result.payload.get("success")
                            )
                tool_payload = self._format_tool_result(
                    tool_name, result.payload, result.duration_ms, "ok"
                )
                result_summary = self._extract_result_summary(tool_name, result.payload)

                invocation_record: dict[str, Any] = {
                    "tool": tool_name,
                    "payload": payload_before_invoke,
                }
                if payload != payload_before_invoke:
                    invocation_record["effective_payload"] = dict(payload)
                if auto_retry_details:
                    invocation_record["auto_retry"] = auto_retry_details
                if write_interaction_metadata:
                    invocation_record["knowledge_interaction"] = write_interaction_metadata
                if call_id:
                    invocation_record["call_id"] = call_id
                invocations.append(invocation_record)

                progress_info: dict[str, Any] = {
                    "status": "tool_invoked",
                    "tool": tool_name,
                    "batch_size": current_batch_size,
                    "tool_calls_done": iteration_count,
                    "tool_calls_cap": int(max_tool_invocations),
                    "tool_calls_remaining": max(
                        0, max_tool_invocations - iteration_count
                    ),
                    "call_id": call_id,
                }
                if result_summary:
                    progress_info["result_summary"] = result_summary
                if callable(emit_progress):
                    emit_progress(progress_info)

                self._logger.info(
                    "[mcp_orchestrator] Tool invocation #%d: tool=%s",
                    iteration_count,
                    tool_name,
                )
            except Exception as exc:
                tool_payload = self._format_tool_result(
                    tool_name, None, None, "error", str(exc)
                )
                error_record: dict[str, Any] = {
                    "tool": tool_name,
                    "payload": dict(payload),
                    "error": str(exc),
                }
                if write_interaction_metadata:
                    error_record["knowledge_interaction"] = write_interaction_metadata
                if call_id:
                    error_record["call_id"] = call_id
                invocations.append(error_record)
                if callable(emit_progress):
                    emit_progress(
                        {
                            "status": "tool_failed",
                            "tool": tool_name,
                            "batch_size": current_batch_size,
                            "tool_calls_done": iteration_count,
                            "tool_calls_cap": int(max_tool_invocations),
                            "tool_calls_remaining": max(
                                0, max_tool_invocations - iteration_count
                            ),
                            "call_id": call_id,
                            "error": str(exc),
                        }
                    )
                self._logger.warning(
                    "[mcp_orchestrator] Tool %s failed: %s", tool_name, exc
                )

            augmented_context.append({"role": "tool", "content": tool_payload})
            tool_messages.append({"role": "tool", "content": tool_payload})

        # Store remaining overflow tool calls for the backfill handler.
        data["allowed_write_tools"] = allowed_write_tools
        data["write_policy_reason"] = write_policy_reason
        return WorkflowActionResult(
            outputs={
                "tool_execution_complete": True,
                "iteration_count": iteration_count,
                "remaining_tool_calls": remaining_tool_calls,
            }
        )

    def _action_tool_calling_backfill(self, request: Any) -> WorkflowActionResult:
        """Summariser LLM call after tool execution; detect chained tool calls.

        If there are remaining overflow tool calls (from batch capping),
        they are returned directly without an LLM call.  Otherwise a
        summariser call asks the LLM to produce a user-facing response or
        additional tool calls.

        Outputs:
            more_tool_calls (bool): True if chained tool calls were found.
            tool_calls (list | None): New tool calls (if any).
            tool_calls_present (bool): Same as more_tool_calls.
            final_response (str | None): Final text response (if done).
        """
        data = request.data
        env = request.environment
        llm_client = env.llm_client
        augmented_context = data["augmented_context"]
        policy_state = data["policy_state"]
        registry_snapshot = data.get("registry_snapshot")
        user_concept_id = data.get("user_concept_id")
        org_concept_id = data.get("org_concept_id")
        model_for_stage = data["model_for_stage"]
        record_llm_call = data["record_llm_call"]
        emit_progress_raw = data.get("emit_progress")
        emit_progress_cb: Callable[[Mapping[str, Any]], None] | None = (
            cast(Callable[[Mapping[str, Any]], None], emit_progress_raw)
            if callable(emit_progress_raw)
            else None
        )
        aux_llm_calls = data["aux_llm_calls"]
        llm_calls = data["llm_calls"]
        iteration_count = data.get("iteration_count", 0)
        max_tool_invocations = env.max_tool_invocations or 8
        missing_tool_call_retry_attempts = self._coerce_non_negative_int(
            data.get("missing_tool_call_retry_attempts"),
            default=0,
            max_value=20,
        )
        missing_tool_call_retry_budget = self._coerce_non_negative_int(
            data.get("missing_tool_call_retry_budget"),
            default=self._max_missing_tool_call_retries_per_turn,
            max_value=20,
        )
        missing_tool_call_retry_suppressed = bool(
            data.get("missing_tool_call_retry_suppressed")
        )

        # If there are overflow tool calls from batch capping, return them
        # directly as chained calls (no summariser LLM call needed).
        remaining = data.get("remaining_tool_calls") or []
        if remaining and iteration_count < max_tool_invocations:
            return WorkflowActionResult(
                outputs={
                    "more_tool_calls": True,
                    "tool_calls_present": True,
                    "tool_calls_validated": False,
                    "tool_calls": remaining,
                    "remaining_tool_calls": [],
                    "missing_tool_call_retry_attempts": missing_tool_call_retry_attempts,
                    "missing_tool_call_retry_budget": missing_tool_call_retry_budget,
                    "missing_tool_call_retry_remaining": max(
                        0,
                        missing_tool_call_retry_budget
                        - missing_tool_call_retry_attempts,
                    ),
                    "missing_tool_call_retry_suppressed": missing_tool_call_retry_suppressed,
                    "result": True,
                }
            )

        # Summariser LLM call.
        follow_up_prompt = (
            "Provide a final answer to the user now that the tool result is available. "
            "If the tool failed, explain the error. "
            "If you need to call another tool, you may do so."
        )
        summariser_model = model_for_stage("summariser")
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
            record_llm_call=record_llm_call,
            emit_progress=emit_progress_cb,
        )

        # Check if the summariser response contains more tool calls.
        if iteration_count < max_tool_invocations:
            interpretation = self._interpret_model_turn(current_response)
            if interpretation.tool_calls:
                return WorkflowActionResult(
                    outputs={
                        "more_tool_calls": True,
                        "tool_calls_present": True,
                        "tool_calls_validated": False,
                        "tool_calls": interpretation.tool_calls,
                        "current_response": current_response,
                        "remaining_tool_calls": [],
                        "missing_tool_call_retry_attempts": missing_tool_call_retry_attempts,
                        "missing_tool_call_retry_budget": missing_tool_call_retry_budget,
                        "missing_tool_call_retry_remaining": max(
                            0,
                            missing_tool_call_retry_budget
                            - missing_tool_call_retry_attempts,
                        ),
                        "missing_tool_call_retry_suppressed": missing_tool_call_retry_suppressed,
                        "result": True,
                    }
                )

            method_catalogue_for_requirements = data.get("method_catalogue")
            if not isinstance(method_catalogue_for_requirements, Mapping):
                try:
                    method_catalogue_for_requirements = self._gateway.describe_methods()
                except Exception:
                    method_catalogue_for_requirements = None

            prompt_requirement_state = self._derive_prompt_tool_requirements(
                data.get("prompt"),
                method_catalogue=(
                    method_catalogue_for_requirements
                    if isinstance(method_catalogue_for_requirements, Mapping)
                    else None
                ),
            )
            required_prompt_tools = list(
                cast(list[str], prompt_requirement_state.get("required_tools") or [])
            )
            required_prompt_fetch_concept_ids = list(
                cast(
                    list[str],
                    prompt_requirement_state.get("required_fetch_concept_ids") or [],
                )
            )
            required_prompt_create_type_name_raw = prompt_requirement_state.get(
                "required_create_type_name"
            )
            required_prompt_create_type_name = (
                str(required_prompt_create_type_name_raw).strip()
                if isinstance(required_prompt_create_type_name_raw, str)
                else None
            )
            data["required_prompt_tools"] = list(required_prompt_tools)
            data["required_prompt_fetch_concept_ids"] = list(
                required_prompt_fetch_concept_ids
            )
            data["required_prompt_create_type_name"] = required_prompt_create_type_name

            invocations_for_requirements = cast(
                Sequence[Mapping[str, Any]], data.get("invocations") or []
            )
            missing_prompt_tools, missing_prompt_fetch_concept_ids = (
                self._derive_missing_prompt_requirements(
                    required_tools=required_prompt_tools,
                    required_fetch_concept_ids=required_prompt_fetch_concept_ids,
                    tool_invocations=invocations_for_requirements,
                )
            )
            data["missing_prompt_tools"] = list(missing_prompt_tools)
            data["missing_prompt_fetch_concept_ids"] = list(
                missing_prompt_fetch_concept_ids
            )
            missing_retry_reason = self._build_missing_prompt_retry_reason(
                missing_tools=missing_prompt_tools,
                missing_fetch_concept_ids=missing_prompt_fetch_concept_ids,
            )
            if missing_retry_reason:
                data["missing_tool_call_retry_reason_override"] = missing_retry_reason
            else:
                data.pop("missing_tool_call_retry_reason_override", None)

            if isinstance(aux_llm_calls, list) and required_prompt_tools:
                try:
                    aux_llm_calls.append(
                        {
                            "type": "prompt_tool_requirements",
                            "required_tools": list(required_prompt_tools),
                            "missing_tools": list(missing_prompt_tools),
                            "required_fetch_concept_ids": list(
                                required_prompt_fetch_concept_ids
                            ),
                            "missing_fetch_concept_ids": list(
                                missing_prompt_fetch_concept_ids
                            ),
                            "required_create_type_name": (
                                required_prompt_create_type_name
                            ),
                            "invoked_tools": [
                                str(item.get("tool"))
                                for item in invocations_for_requirements
                                if isinstance(item.get("tool"), str)
                            ],
                        }
                    )
                except Exception:
                    pass

            auto_proceed_enabled = bool(
                data.get("auto_proceed_minimal_imposition_enabled", True)
            )
            auto_proceed_assessment = self._assess_minimal_imposition_auto_proceed(
                current_response
            )
            auto_proceed_gate_passed = auto_proceed_enabled and bool(
                auto_proceed_assessment.get("should_auto_proceed")
            )

            if isinstance(aux_llm_calls, list):
                try:
                    aux_llm_calls.append(
                        {
                            "type": "auto_proceed_minimal_imposition",
                            "enabled": auto_proceed_enabled,
                            "should_auto_proceed": bool(
                                auto_proceed_assessment.get("should_auto_proceed")
                            ),
                            "reason": str(auto_proceed_assessment.get("reason") or ""),
                            "has_progress_promise": bool(
                                auto_proceed_assessment.get("has_progress_promise")
                            ),
                            "has_intent_language": bool(
                                auto_proceed_assessment.get("has_intent_language")
                            ),
                            "has_remaining_work_signal": bool(
                                auto_proceed_assessment.get(
                                    "has_remaining_work_signal"
                                )
                            ),
                            "asks_for_user_decision": bool(
                                auto_proceed_assessment.get("asks_for_user_decision")
                            ),
                            "contains_question_mark": bool(
                                auto_proceed_assessment.get("contains_question_mark")
                            ),
                        }
                    )
                except Exception:
                    pass

            exc = interpretation.tool_call_parse_error
            recovery_reason: str | None = None
            override_retry_reason = data.get("missing_tool_call_retry_reason_override")
            has_override_retry_reason = isinstance(
                override_retry_reason, str
            ) and bool(str(override_retry_reason).strip())
            if exc is not None:
                recovery_reason = "parse_error"
            elif has_override_retry_reason:
                recovery_reason = "required_prompt_tools_missing"
            elif auto_proceed_gate_passed:
                recovery_reason = "minimal_imposition"

            if recovery_reason is not None:
                recovery_data = self._run_missing_tool_call_recovery_workflow(
                    request=request,
                    environment=env,
                    data=data,
                    response_text=(
                        current_response
                        if isinstance(current_response, str)
                        else str(current_response)
                    ),
                    interpretation=interpretation,
                    use_structured=False,
                    tool_call_parse_error=exc,
                    tool_calls=interpretation.tool_calls,
                    default_model=summariser_model,
                )

                recovered_calls = None
                if recovery_data is not None:
                    current_response = recovery_data.get(
                        "response_text",
                        current_response,
                    )
                    recovered_calls = recovery_data.get("tool_calls")
                    exc = recovery_data.get("tool_call_parse_error", exc)
                    missing_tool_call_retry_attempts = self._coerce_non_negative_int(
                        recovery_data.get(
                            "missing_tool_call_retry_attempts",
                            missing_tool_call_retry_attempts,
                        ),
                        default=missing_tool_call_retry_attempts,
                        max_value=20,
                    )
                    missing_tool_call_retry_budget = self._coerce_non_negative_int(
                        recovery_data.get(
                            "missing_tool_call_retry_budget",
                            missing_tool_call_retry_budget,
                        ),
                        default=missing_tool_call_retry_budget,
                        max_value=20,
                    )
                    missing_tool_call_retry_suppressed = bool(
                        recovery_data.get("missing_tool_call_retry_suppressed")
                    )
                    data["missing_tool_call_retry_attempts"] = (
                        missing_tool_call_retry_attempts
                    )
                    data["missing_tool_call_retry_budget"] = (
                        missing_tool_call_retry_budget
                    )
                    data["missing_tool_call_retry_suppressed"] = (
                        missing_tool_call_retry_suppressed
                    )
                    if recovered_calls:
                        return WorkflowActionResult(
                            outputs={
                                "more_tool_calls": True,
                                "tool_calls_present": True,
                                "tool_calls_validated": False,
                                "tool_calls": recovered_calls,
                                "current_response": current_response,
                                "remaining_tool_calls": [],
                                "missing_tool_call_retry_attempts": missing_tool_call_retry_attempts,
                                "missing_tool_call_retry_budget": missing_tool_call_retry_budget,
                                "missing_tool_call_retry_remaining": max(
                                    0,
                                    missing_tool_call_retry_budget
                                    - missing_tool_call_retry_attempts,
                                ),
                                "missing_tool_call_retry_suppressed": missing_tool_call_retry_suppressed,
                                "result": True,
                            }
                        )

                if interpretation.tool_call_parse_error is not None:
                    build_error = data.get("build_parse_error_result")
                    if callable(build_error):
                        invocations = data.get("invocations") or []
                        tool_messages = data.get("tool_messages") or []
                        error_result = build_error(
                            exc,
                            invocations_override=tuple(invocations),
                            tool_messages_override=tuple(tool_messages),
                        )
                        return WorkflowActionResult(
                            outputs={
                                "orchestrator_result": error_result,
                                "missing_tool_call_retry_attempts": missing_tool_call_retry_attempts,
                                "missing_tool_call_retry_budget": missing_tool_call_retry_budget,
                                "missing_tool_call_retry_remaining": max(
                                    0,
                                    missing_tool_call_retry_budget
                                    - missing_tool_call_retry_attempts,
                                ),
                                "missing_tool_call_retry_suppressed": missing_tool_call_retry_suppressed,
                                "result": False,
                            }
                        )

                    # Parse error during chained extraction — treat as done
                    # rather than failing the whole flow.
                    self._logger.debug(
                        "[mcp_orchestrator] Chained tool-call extraction failed; "
                        "treating summariser response as final."
                    )

        # Log if we hit the iteration limit.
        if iteration_count >= max_tool_invocations and self._is_json_action_response(
            current_response
        ):
            self._logger.warning(
                "[mcp_orchestrator] Reached max tool invocation limit (%d), "
                "but LLM still wants to call tools.",
                max_tool_invocations,
            )
            emit_progress = data.get("emit_progress")
            if callable(emit_progress):
                emit_progress(
                    {
                        "status": "tool_limit_reached",
                        "tool_calls_done": iteration_count,
                        "tool_calls_cap": int(max_tool_invocations),
                        "tool_calls_remaining": 0,
                    }
                )

        return WorkflowActionResult(
            outputs={
                "more_tool_calls": False,
                "tool_calls_present": False,
                "final_response": current_response,
                "current_response": current_response,
                "missing_tool_call_retry_attempts": missing_tool_call_retry_attempts,
                "missing_tool_call_retry_budget": missing_tool_call_retry_budget,
                "missing_tool_call_retry_remaining": max(
                    0,
                    missing_tool_call_retry_budget - missing_tool_call_retry_attempts,
                ),
                "missing_tool_call_retry_suppressed": missing_tool_call_retry_suppressed,
                "result": False,
            }
        )

    def _action_turn_execution_critic(self, request: Any) -> WorkflowActionResult:
        """Build turn execution evidence and postcondition checks."""

        data = request.data
        env = request.environment

        prompt_text = data.get("prompt")
        if not isinstance(prompt_text, str):
            prompt_text = str(prompt_text or "")

        response_text = data.get("final_response")
        if not isinstance(response_text, str):
            current_response = data.get("current_response")
            response_text = (
                current_response if isinstance(current_response, str) else ""
            )

        raw_tool_invocations = data.get("invocations")
        tool_invocations: Sequence[Mapping[str, Any]]
        if isinstance(raw_tool_invocations, (list, tuple)):
            tool_invocations = cast(Sequence[Mapping[str, Any]], raw_tool_invocations)
        else:
            tool_invocations = ()

        raw_aux = data.get("aux_llm_calls")
        aux_calls: Sequence[Mapping[str, Any]]
        if isinstance(raw_aux, list):
            aux_calls = cast(Sequence[Mapping[str, Any]], raw_aux)
        else:
            aux_calls = ()

        workflow_discovery = data.get("workflow_discovery_result")
        workflow_discovery_payload = (
            dict(workflow_discovery) if isinstance(workflow_discovery, Mapping) else None
        )
        workflow_routing = data.get("workflow_routing")
        workflow_routing_payload = (
            dict(workflow_routing) if isinstance(workflow_routing, Mapping) else None
        )

        turn_execution_record = build_turn_execution_record(
            request_id=data.get("turn_id"),
            session_id=data.get("conversation_session_id"),
            namespace=env.user_namespace,
            actor_concept_id=data.get("actor_concept_id") or data.get("user_concept_id"),
            user_id=data.get("user_concept_id"),
            org_id=data.get("org_concept_id"),
            prompt_text=prompt_text,
            response_text=response_text,
            interaction_timestamp_utc=data.get("turn_execution_record_generated_at"),
            workflow_discovery=workflow_discovery_payload,
            workflow_routing=workflow_routing_payload,
            tool_invocations=tool_invocations,
            turn_execution_diagnostics=data.get("turn_execution_diagnostics"),
            aux_llm_calls=aux_calls,
        )

        completion_gate = turn_execution_record.get("completion_gate")
        gate_requires_follow_up = False
        gate_decision = None
        gate_safe_to_claim = True
        if isinstance(completion_gate, Mapping):
            gate_requires_follow_up = bool(
                completion_gate.get("requires_follow_up", False)
            )
            raw_gate_decision = completion_gate.get("decision")
            if isinstance(raw_gate_decision, str) and raw_gate_decision.strip():
                gate_decision = raw_gate_decision.strip()
            gate_safe_to_claim = bool(
                completion_gate.get("safe_to_claim_completion", True)
            )

        critic_summary: Mapping[str, Any] = {}
        critic_payload = turn_execution_record.get("critic")
        if isinstance(critic_payload, Mapping):
            raw_summary = critic_payload.get("summary")
            if isinstance(raw_summary, Mapping):
                critic_summary = dict(raw_summary)

        required_effects = turn_execution_record.get("required_effects")
        if not isinstance(required_effects, list):
            required_effects = []
        postcondition_checks = turn_execution_record.get("postcondition_checks")
        if not isinstance(postcondition_checks, list):
            postcondition_checks = []

        if isinstance(raw_aux, list):
            try:
                raw_aux.append(
                    {
                        "type": "turn_execution_critic",
                        "workflow_id": KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
                        "decision_preview": gate_decision,
                        "requires_follow_up": gate_requires_follow_up,
                        "required_effect_count": len(required_effects),
                        "postcondition_check_count": len(postcondition_checks),
                        "request_id": turn_execution_record.get("request_id"),
                    }
                )
            except Exception:
                pass

        return WorkflowActionResult(
            outputs={
                "turn_execution_record": turn_execution_record,
                "required_effects": list(required_effects),
                "postcondition_checks": list(postcondition_checks),
                "critic_summary": dict(critic_summary),
                "completion_gate_decision": gate_decision,
                "completion_gate_requires_follow_up": gate_requires_follow_up,
                "completion_gate_safe_to_claim_completion": gate_safe_to_claim,
            }
        )

    def _action_turn_execution_completion_gate(
        self, request: Any
    ) -> WorkflowActionResult:
        """Apply completion gate policy and annotate unresolved execution."""

        data = request.data
        record = data.get("turn_execution_record")
        if not isinstance(record, Mapping):
            # Rebuild critic payload defensively when gate is invoked directly.
            critic_result = self._action_turn_execution_critic(request)
            rebuilt_record = critic_result.outputs.get("turn_execution_record")
            record = rebuilt_record if isinstance(rebuilt_record, Mapping) else {}

        completion_gate_payload = record.get("completion_gate")
        if not isinstance(completion_gate_payload, Mapping):
            completion_gate_payload = {}

        decision = completion_gate_payload.get("decision")
        if not isinstance(decision, str) or not decision.strip():
            decision = "completed"
        decision = decision.strip()

        decision_reason = completion_gate_payload.get("decision_reason")
        if not isinstance(decision_reason, str):
            decision_reason = ""
        decision_reason = decision_reason.strip()

        blocking_effect_ids_raw = completion_gate_payload.get("blocking_effect_ids")
        blocking_effect_ids: list[str] = []
        if isinstance(blocking_effect_ids_raw, list):
            for item in blocking_effect_ids_raw:
                if isinstance(item, str) and item.strip():
                    blocking_effect_ids.append(item.strip())

        safe_to_claim_completion = bool(
            completion_gate_payload.get("safe_to_claim_completion", True)
        )
        requires_follow_up = bool(
            completion_gate_payload.get("requires_follow_up", False)
        )

        final_response = data.get("final_response")
        if not isinstance(final_response, str):
            current_response = data.get("current_response")
            final_response = (
                current_response if isinstance(current_response, str) else ""
            )

        if not safe_to_claim_completion:
            if decision == "failed":
                status_line = "Execution status: requested mutation failed or was blocked."
            elif decision == "escalation_required":
                status_line = "Execution status: requested mutation was not executed."
            elif decision == "partial":
                status_line = "Execution status: mutation may have run but verification is inconclusive."
            else:
                status_line = "Execution status: follow-up verification is required."

            if decision_reason:
                status_line = f"{status_line} {decision_reason}"
            if blocking_effect_ids:
                status_line = (
                    f"{status_line} Blocking effect IDs: {', '.join(blocking_effect_ids)}."
                )

            if isinstance(final_response, str) and final_response.strip():
                if "Execution status:" not in final_response:
                    final_response = f"{final_response.rstrip()}\n\n{status_line}"
            else:
                final_response = status_line

        aux_llm_calls = data.get("aux_llm_calls")
        if isinstance(aux_llm_calls, list):
            try:
                aux_llm_calls.append(
                    {
                        "type": "turn_completion_gate",
                        "workflow_id": TURN_COMPLETION_GATE_WORKFLOW_ID,
                        "decision": decision,
                        "decision_reason": decision_reason,
                        "safe_to_claim_completion": safe_to_claim_completion,
                        "requires_follow_up": requires_follow_up,
                        "blocking_effect_ids": list(blocking_effect_ids),
                    }
                )
            except Exception:
                pass

        return WorkflowActionResult(
            outputs={
                "final_response": final_response,
                "completion_gate_decision": decision,
                "completion_gate_decision_reason": decision_reason,
                "completion_gate_blocking_effect_ids": list(blocking_effect_ids),
                "completion_gate_safe_to_claim_completion": safe_to_claim_completion,
                "completion_gate_requires_follow_up": requires_follow_up,
                "result": requires_follow_up,
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
                "CONCEPT ID CANONICALISATION (MUST FOLLOW):\n"
                "- Concept IDs are canonicalised: lowercase, non-alphanumeric runs become single underscore\n"
                '- CamelCase is collapsed: "BusinessTrip" → #V#businesstrip\n'
                '- Spaces become underscores: "Business Trip" → #V#business_trip\n'
                '- Hyphens become underscores: "Business-Trip" → #V#business_trip\n'
                "- ALWAYS use the concept_id from tool responses for subsequent operations\n"
                '- If creation fails with "already exists", use the canonical_id from the error response\n'
                "- Before referencing parent types, use fetch_concept or concept_exists to get the exact ID\n"
                "- DO NOT guess IDs - verify them first\n\n"
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
    def _strip_fenced_code_blocks(text: str) -> tuple[str, bool]:
        """Strip markdown fenced code blocks for safer claim detection."""

        if not isinstance(text, str) or not text:
            return "", False
        # Handles both closed and unterminated fences by treating the
        # remainder as code content.
        stripped, substitutions = re.subn(
            r"```[\w+\-]*\n.*?(?:```|$)",
            "",
            text,
            flags=re.DOTALL,
        )
        return stripped, substitutions > 0

    @classmethod
    def _extract_completion_claim_candidates(
        cls,
        response_text: str,
        *,
        max_claims: int | None = None,
    ) -> tuple[list[str], Mapping[str, Any]]:
        """Extract likely completion claims from assistant prose.

        Claims inside markdown code fences are ignored by design.
        """

        if not isinstance(response_text, str) or not response_text.strip():
            return (
                [],
                {
                    "code_fence_stripped": False,
                    "text_chars_considered": 0,
                    "segment_count": 0,
                    "claim_count": 0,
                    "claim_cap": 0,
                },
            )

        claim_cap = cls._COMPLETION_CLAIM_MAX_CANDIDATES
        if max_claims is not None:
            try:
                claim_cap = int(max_claims)
            except Exception:
                claim_cap = cls._COMPLETION_CLAIM_MAX_CANDIDATES
        claim_cap = max(1, min(20, claim_cap))

        stripped_text, stripped_any_fence = cls._strip_fenced_code_blocks(
            response_text
        )
        searchable = stripped_text[: cls._COMPLETION_CLAIM_MAX_TEXT_CHARS]
        segments: list[str] = []
        for raw_line in searchable.splitlines():
            line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", raw_line).strip()
            if not line:
                continue
            parts = re.split(r"(?<=[.!?])\s+", line)
            for part in parts:
                sentence = " ".join(part.strip().split())
                if sentence:
                    segments.append(sentence)

        claims: list[str] = []
        seen_claims: set[str] = set()
        for segment in segments:
            lowered = segment.lower()
            if len(lowered) < 12:
                continue
            if cls._COMPLETION_CLAIM_INTENT_PATTERN.search(lowered):
                continue
            if not (
                cls._COMPLETION_CLAIM_VERB_PATTERN.search(lowered)
                or cls._COMPLETION_CLAIM_LINE_START_PATTERN.search(lowered)
            ):
                continue
            key = lowered[:400]
            if key in seen_claims:
                continue
            seen_claims.add(key)
            claims.append(segment[:400])
            if len(claims) >= claim_cap:
                break

        return (
            claims,
            {
                "code_fence_stripped": stripped_any_fence,
                "text_chars_considered": len(searchable),
                "segment_count": len(segments),
                "claim_count": len(claims),
                "claim_cap": claim_cap,
            },
        )

    @staticmethod
    def _summarise_tool_invocation_outcomes(
        tool_invocations: Sequence[Mapping[str, Any]],
    ) -> tuple[list[str], list[str]]:
        """Return (successful_tools, failed_tools) preserving first-seen order."""

        successful: list[str] = []
        failed: list[str] = []
        seen_success: set[str] = set()
        seen_failed: set[str] = set()

        for invocation in tool_invocations:
            if not isinstance(invocation, Mapping):
                continue
            raw_name = invocation.get("tool")
            if not isinstance(raw_name, str):
                continue
            tool_name = raw_name.strip()
            if not tool_name or tool_name.startswith("__"):
                continue

            error_value = invocation.get("error")
            payload = invocation.get("payload")
            status_value = ""
            if isinstance(payload, Mapping):
                raw_status = payload.get("status")
                if isinstance(raw_status, str):
                    status_value = raw_status.strip().lower()

            failed_invocation = bool(
                (isinstance(error_value, str) and error_value.strip())
                or status_value in {"error", "failed", "failure"}
            )
            lowered = tool_name.lower()
            if failed_invocation:
                if lowered not in seen_failed:
                    failed.append(tool_name)
                    seen_failed.add(lowered)
                continue
            if lowered not in seen_success:
                successful.append(tool_name)
                seen_success.add(lowered)

        return successful, failed

    @staticmethod
    def _normalise_concept_id_candidate(raw_value: Any) -> str | None:
        """Normalise #V# concept IDs (or concept slug names) to canonical form."""

        if not isinstance(raw_value, str):
            return None
        value = raw_value.strip().rstrip(".,;:)")
        if not value:
            return None

        lowered = value.lower()
        if lowered.startswith("#v#"):
            return "#V#" + value[3:]
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,}", value):
            return f"#V#{value}"
        return None

    @classmethod
    def _extract_required_fetch_concept_ids_from_prompt(
        cls,
        user_prompt: Any,
    ) -> list[str]:
        """Extract concept IDs/slugs that imply required fetch_concept checks."""

        if not isinstance(user_prompt, str) or not user_prompt.strip():
            return []

        # Require multiple suffix segments so field names like
        # "workflow_mapping_spec" are not treated as concept IDs.
        matches = re.findall(
            r"(#V#workflow_mapping_[A-Za-z0-9]+(?:_[A-Za-z0-9]+){2,}"
            r"|\bworkflow_mapping_[A-Za-z0-9]+(?:_[A-Za-z0-9]+){2,}\b)",
            user_prompt,
            flags=re.IGNORECASE,
        )
        concept_ids: list[str] = []
        seen: set[str] = set()
        for match in matches:
            concept_id = cls._normalise_concept_id_candidate(match)
            if not concept_id:
                continue
            key = concept_id.lower()
            if key in seen:
                continue
            seen.add(key)
            concept_ids.append(concept_id)
        return concept_ids

    @classmethod
    def _extract_required_create_type_name_from_prompt(
        cls,
        user_prompt: Any,
    ) -> str | None:
        """Extract a requested test type name for deterministic create_concepts."""

        if not isinstance(user_prompt, str) or not user_prompt.strip():
            return None
        lowered = user_prompt.lower()
        if "create" not in lowered or "type" not in lowered:
            return None

        pattern = re.compile(
            r"(?:#V#)?(test_workflow_trigger_type_[A-Za-z0-9_]+)",
            flags=re.IGNORECASE,
        )
        match = pattern.search(user_prompt)
        if not match:
            return None

        raw_name = str(match.group(1) or "").strip()
        if not raw_name:
            return None
        return raw_name.lstrip("#").lstrip("V#")

    @classmethod
    def _extract_explicit_prompt_tool_requirements(
        cls,
        user_prompt: Any,
        *,
        method_catalogue: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Return tool names explicitly requested via "call <tool>" phrases."""

        if not isinstance(user_prompt, str) or not user_prompt.strip():
            return []

        catalogue_lookup: dict[str, str] = {}
        if isinstance(method_catalogue, Mapping):
            for tool_name in method_catalogue.keys():
                if isinstance(tool_name, str) and tool_name.strip():
                    canonical = tool_name.strip()
                    catalogue_lookup[canonical.lower()] = canonical

        required_tools: list[str] = []
        seen: set[str] = set()
        for match in cls._PROMPT_EXPLICIT_TOOL_CALL_PATTERN.finditer(user_prompt):
            candidate = str(match.group(1) or "").strip()
            if not candidate:
                continue
            lowered = candidate.lower()
            if catalogue_lookup and lowered not in catalogue_lookup:
                continue
            resolved = catalogue_lookup.get(lowered, candidate)
            key = resolved.lower()
            if key in seen:
                continue
            seen.add(key)
            required_tools.append(resolved)

        return required_tools

    @staticmethod
    def _missing_prompt_tool_requirements(
        *,
        required_tools: Sequence[str],
        tool_invocations: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        """Return required prompt tools not yet seen in invocation history."""

        if not required_tools:
            return []

        observed_tools: set[str] = set()
        for invocation in tool_invocations:
            if not isinstance(invocation, Mapping):
                continue
            raw_tool = invocation.get("tool")
            if isinstance(raw_tool, str) and raw_tool.strip():
                observed_tools.add(raw_tool.strip().lower())

        return [
            tool_name
            for tool_name in required_tools
            if isinstance(tool_name, str)
            and tool_name.strip()
            and tool_name.strip().lower() not in observed_tools
        ]

    @classmethod
    def _extract_fetch_concept_ids_from_invocations(
        cls,
        tool_invocations: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        """Collect concept IDs already fetched via fetch_concept calls."""

        concept_ids: list[str] = []
        seen: set[str] = set()

        for invocation in tool_invocations:
            if not isinstance(invocation, Mapping):
                continue
            raw_tool = invocation.get("tool")
            if not isinstance(raw_tool, str) or raw_tool.strip().lower() != "fetch_concept":
                continue

            payload = invocation.get("effective_payload")
            if not isinstance(payload, Mapping):
                payload = invocation.get("payload")
            if not isinstance(payload, Mapping):
                continue

            concept_id = cls._normalise_concept_id_candidate(payload.get("concept_id"))
            if not concept_id:
                continue
            key = concept_id.lower()
            if key in seen:
                continue
            seen.add(key)
            concept_ids.append(concept_id)

        return concept_ids

    @classmethod
    def _derive_prompt_tool_requirements(
        cls,
        user_prompt: Any,
        *,
        method_catalogue: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Derive deterministic prompt requirements for tool recovery."""

        required_tools = cls._extract_explicit_prompt_tool_requirements(
            user_prompt,
            method_catalogue=method_catalogue,
        )

        available_tools: set[str] = set()
        if isinstance(method_catalogue, Mapping):
            for tool_name in method_catalogue.keys():
                if isinstance(tool_name, str) and tool_name.strip():
                    available_tools.add(tool_name.strip().lower())

        def _tool_available(tool_name: str) -> bool:
            if not available_tools:
                return True
            return tool_name.lower() in available_tools

        required_fetch_concept_ids = cls._extract_required_fetch_concept_ids_from_prompt(
            user_prompt
        )
        if not _tool_available("fetch_concept"):
            required_fetch_concept_ids = []

        required_create_type_name = cls._extract_required_create_type_name_from_prompt(
            user_prompt
        )
        if not _tool_available("create_concepts"):
            required_create_type_name = None

        seen_required = {
            str(tool_name).strip().lower()
            for tool_name in required_tools
            if isinstance(tool_name, str) and tool_name.strip()
        }
        if required_create_type_name and "create_concepts" not in seen_required:
            required_tools.append("create_concepts")
            seen_required.add("create_concepts")
        if required_fetch_concept_ids and "fetch_concept" not in seen_required:
            required_tools.append("fetch_concept")

        return {
            "required_tools": required_tools,
            "required_fetch_concept_ids": required_fetch_concept_ids,
            "required_create_type_name": required_create_type_name,
        }

    @classmethod
    def _derive_missing_prompt_requirements(
        cls,
        *,
        required_tools: Sequence[str],
        required_fetch_concept_ids: Sequence[str],
        tool_invocations: Sequence[Mapping[str, Any]],
    ) -> tuple[list[str], list[str]]:
        """Determine missing prompt requirements from invocation history."""

        missing_tools = cls._missing_prompt_tool_requirements(
            required_tools=required_tools,
            tool_invocations=tool_invocations,
        )

        missing_fetch_concept_ids: list[str] = []
        if required_fetch_concept_ids:
            fetched_concept_ids = cls._extract_fetch_concept_ids_from_invocations(
                tool_invocations
            )
            fetched_lookup = {concept_id.lower() for concept_id in fetched_concept_ids}
            for concept_id in required_fetch_concept_ids:
                if (
                    isinstance(concept_id, str)
                    and concept_id.strip()
                    and concept_id.lower() not in fetched_lookup
                ):
                    missing_fetch_concept_ids.append(concept_id)

        if missing_fetch_concept_ids:
            missing_lookup = {
                tool_name.lower()
                for tool_name in missing_tools
                if isinstance(tool_name, str) and tool_name.strip()
            }
            if "fetch_concept" not in missing_lookup:
                missing_tools.append("fetch_concept")

        return missing_tools, missing_fetch_concept_ids

    @staticmethod
    def _build_missing_prompt_retry_reason(
        *,
        missing_tools: Sequence[str],
        missing_fetch_concept_ids: Sequence[str],
    ) -> str | None:
        """Build a stable retry reason for unmet prompt requirements."""

        missing_names = [
            str(tool_name).strip()
            for tool_name in missing_tools
            if isinstance(tool_name, str) and tool_name.strip()
        ]
        if not missing_names and not missing_fetch_concept_ids:
            return None

        details: list[str] = list(missing_names)
        if missing_fetch_concept_ids:
            details.append(
                "fetch_concept targets: "
                + ", ".join(
                    str(concept_id).strip()
                    for concept_id in missing_fetch_concept_ids
                    if isinstance(concept_id, str) and concept_id.strip()
                )
            )

        if not details:
            return None
        return "prompt requested tool(s) not yet invoked: " + "; ".join(details)

    @classmethod
    def _validate_completion_claims(
        cls,
        *,
        claims: Sequence[str],
        tool_invocations: Sequence[Mapping[str, Any]],
        tool_messages: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Validate completion claims against observed tool execution evidence."""

        successful_tools, failed_tools = cls._summarise_tool_invocation_outcomes(
            tool_invocations
        )
        all_tools = list(successful_tools)
        for tool_name in failed_tools:
            if tool_name.lower() not in {item.lower() for item in all_tools}:
                all_tools.append(tool_name)

        action_hint_map: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
            (("create", "created", "add", "added", "new"), ("create", "add", "insert")),
            (
                ("update", "updated", "edit", "edited", "modify", "modified", "rename"),
                ("update", "edit", "modify", "rename", "set"),
            ),
            (("delete", "deleted", "remove", "removed", "drop"), ("delete", "remove", "drop")),
            (
                ("search", "searched", "fetch", "fetched", "retrieve", "retrieved", "list", "listed"),
                ("search", "fetch", "get", "read", "list"),
            ),
            (("comment", "commented"), ("comment",)),
            (("transition", "transitioned", "status", "moved"), ("transition", "status", "move")),
            (("assign", "assigned"), ("assign",)),
            (("merge", "merged"), ("merge",)),
            (("push", "pushed", "commit", "committed"), ("push", "commit")),
        )

        successful_lookup = {tool.lower(): tool for tool in successful_tools}
        validated: list[dict[str, Any]] = []
        verified: list[dict[str, Any]] = []
        not_verified: list[dict[str, Any]] = []

        for claim in claims:
            lowered_claim = claim.lower()

            mentioned_tools = [
                tool for tool in all_tools if tool.lower() in lowered_claim
            ]

            hinted_tools: list[str] = []
            for claim_tokens, tool_tokens in action_hint_map:
                if not any(token in lowered_claim for token in claim_tokens):
                    continue
                for tool_name in all_tools:
                    lowered_tool = tool_name.lower()
                    if any(token in lowered_tool for token in tool_tokens):
                        hinted_tools.append(tool_name)

            candidate_tools: list[str] = []
            for tool_name in mentioned_tools + hinted_tools:
                if tool_name.lower() not in {t.lower() for t in candidate_tools}:
                    candidate_tools.append(tool_name)

            if mentioned_tools:
                strategy = "explicit_tool_name_match"
            elif candidate_tools:
                strategy = "tool_name_hint_match"
            else:
                strategy = "any_successful_tool"

            matched_success = [
                successful_lookup.get(tool_name.lower())
                for tool_name in candidate_tools
                if tool_name.lower() in successful_lookup
            ]
            matched_success = [item for item in matched_success if isinstance(item, str)]

            if strategy == "any_successful_tool":
                if successful_tools:
                    evidence_tools = successful_tools[:2]
                    record = {
                        "claim": claim,
                        "status": "verified",
                        "strategy": strategy,
                        "evidence": f"Observed successful tool call(s): {', '.join(evidence_tools)}.",
                    }
                    verified.append(record)
                else:
                    record = {
                        "claim": claim,
                        "status": "not_verified",
                        "strategy": strategy,
                        "reason": "No successful tool invocations were recorded for this response.",
                    }
                    not_verified.append(record)
                validated.append(record)
                continue

            if matched_success:
                record = {
                    "claim": claim,
                    "status": "verified",
                    "strategy": strategy,
                    "candidate_tools": list(candidate_tools),
                    "evidence": "Matched successful tool invocation: "
                    + ", ".join(matched_success[:2])
                    + ".",
                }
                verified.append(record)
                validated.append(record)
                continue

            if candidate_tools:
                reason = (
                    "No successful invocation matched the expected tool(s): "
                    + ", ".join(candidate_tools[:3])
                    + "."
                )
            else:
                reason = "No matching tool invocation evidence was found."
            record = {
                "claim": claim,
                "status": "not_verified",
                "strategy": strategy,
                "candidate_tools": list(candidate_tools),
                "reason": reason,
            }
            not_verified.append(record)
            validated.append(record)

        return {
            "validated": validated,
            "verified": verified,
            "not_verified": not_verified,
            "successful_tools": successful_tools,
            "failed_tools": failed_tools,
            "tool_messages_observed": len(tool_messages),
        }

    @staticmethod
    def _render_completion_claim_validation_summary(
        *,
        verified: Sequence[Mapping[str, Any]],
        not_verified: Sequence[Mapping[str, Any]],
    ) -> str:
        if not verified and not not_verified:
            return ""

        lines = ["Completion claim validation summary:", "Verified:"]
        if verified:
            for item in verified:
                claim = str(item.get("claim") or "").strip()
                evidence = str(item.get("evidence") or "").strip()
                if claim and evidence:
                    lines.append(f"- {claim} ({evidence})")
                elif claim:
                    lines.append(f"- {claim}")
        else:
            lines.append("- None")

        lines.append("Not verified:")
        if not_verified:
            for item in not_verified:
                claim = str(item.get("claim") or "").strip()
                reason = str(item.get("reason") or "").strip()
                if claim and reason:
                    lines.append(f"- {claim} ({reason})")
                elif claim:
                    lines.append(f"- {claim}")
        else:
            lines.append("- None")

        return "\n".join(lines)

    def _apply_completion_claim_validation(
        self,
        response_text: str,
        *,
        tool_invocations: Sequence[Mapping[str, Any]],
        tool_messages: Sequence[Mapping[str, Any]],
        aux_log: list[Mapping[str, Any]] | None,
    ) -> str:
        """Append a verified/not-verified summary for likely completion claims."""

        if not isinstance(response_text, str) or not response_text.strip():
            return response_text if isinstance(response_text, str) else str(response_text)

        try:
            claims, detection = self._extract_completion_claim_candidates(response_text)

            if isinstance(aux_log, list):
                aux_log.append(
                    {
                        "type": "completion_claim_detection",
                        "claim_count": len(claims),
                        **dict(detection),
                    }
                )

            if not claims:
                return response_text

            validation = self._validate_completion_claims(
                claims=claims,
                tool_invocations=tool_invocations,
                tool_messages=tool_messages,
            )
            verified = cast(
                Sequence[Mapping[str, Any]], validation.get("verified") or []
            )
            not_verified = cast(
                Sequence[Mapping[str, Any]], validation.get("not_verified") or []
            )

            if isinstance(aux_log, list):
                aux_log.append(
                    {
                        "type": "completion_claim_validation",
                        "claim_count": len(claims),
                        "verified_count": len(verified),
                        "not_verified_count": len(not_verified),
                        "successful_tools": list(validation.get("successful_tools") or []),
                        "failed_tools": list(validation.get("failed_tools") or []),
                        "tool_messages_observed": int(
                            validation.get("tool_messages_observed") or 0
                        ),
                    }
                )

            summary = self._render_completion_claim_validation_summary(
                verified=verified,
                not_verified=not_verified,
            )
            if not summary:
                return response_text

            base = response_text.rstrip()
            if not base:
                return summary
            return f"{base}\n\n{summary}"
        except Exception as exc:
            if isinstance(aux_log, list):
                try:
                    aux_log.append(
                        {
                            "type": "completion_claim_validation",
                            "error": str(exc),
                        }
                    )
                except Exception:
                    pass
            return response_text

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

    @staticmethod
    def _looks_like_concept_id(value: Any) -> bool:
        return (
            isinstance(value, str)
            and value.startswith("#V#")
            and len(value) > 3
            and bool(re.match(r"^#V#[A-Za-z0-9][A-Za-z0-9._-]*$", value))
        )

    def _concept_exists_via_tool(self, concept_id: str) -> bool:
        if not self._looks_like_concept_id(concept_id):
            return False
        try:
            result = self._gateway.invoke("concept_exists", {"concept_id": concept_id})
        except Exception:
            return False

        payload = (
            result.payload
            if hasattr(result, "payload")
            else (result if isinstance(result, Mapping) else None)
        )
        if not isinstance(payload, Mapping):
            return False

        exists = bool(payload.get("exists"))
        accessible = payload.get("accessible")
        if isinstance(accessible, bool):
            return exists and accessible
        return exists

    @staticmethod
    def _candidate_names_from_concept_id(concept_id: str) -> list[str]:
        if not isinstance(concept_id, str):
            return []
        stem = concept_id[3:] if concept_id.startswith("#V#") else concept_id
        stem = re.sub(r"[^A-Za-z0-9._-]+", " ", stem)
        stem = re.sub(r"[._-]+", " ", stem)
        stem = re.sub(r"\s+", " ", stem).strip()
        if not stem:
            return []

        candidates: list[str] = [stem]
        title_case = " ".join(part.capitalize() for part in stem.split(" "))
        if title_case and title_case not in candidates:
            candidates.append(title_case)

        if len(stem.split(" ")) > 1:
            no_article = re.sub(r"^(the|a|an)\s+", "", stem, flags=re.IGNORECASE)
            no_article_title = " ".join(
                part.capitalize() for part in no_article.split(" ")
            )
            for candidate in (no_article, no_article_title):
                if candidate and candidate not in candidates:
                    candidates.append(candidate)

        return candidates

    def _resolve_concept_id_via_tool(
        self,
        name: str,
        *,
        preferred_language: str | None = None,
    ) -> str | None:
        if not isinstance(name, str) or not name.strip():
            return None

        payload: dict[str, Any] = {
            "name": name.strip(),
            "match_code_strings": True,
        }
        if isinstance(preferred_language, str) and preferred_language.strip():
            payload["preferred_languages"] = [preferred_language.strip()]

        try:
            result = self._gateway.invoke("resolve_concept_by_name", payload)
        except Exception:
            return None

        body = (
            result.payload
            if hasattr(result, "payload")
            else (result if isinstance(result, Mapping) else None)
        )
        if not isinstance(body, Mapping):
            return None
        if body.get("status") != "resolved":
            return None

        resolved_id = body.get("resolved_concept_id")
        if not isinstance(resolved_id, str):
            resolved_id = body.get("concept_id")
        if isinstance(resolved_id, str) and self._looks_like_concept_id(resolved_id):
            return resolved_id
        return None

    def _resolve_missing_concept_id(
        self,
        concept_id: str,
        *,
        preferred_language: str | None = None,
        exists_cache: MutableMapping[str, bool] | None = None,
        resolution_cache: MutableMapping[str, str | None] | None = None,
    ) -> str | None:
        if not self._looks_like_concept_id(concept_id):
            return None

        if resolution_cache is not None and concept_id in resolution_cache:
            return resolution_cache[concept_id]

        def _exists(candidate_id: str) -> bool:
            if exists_cache is not None and candidate_id in exists_cache:
                return bool(exists_cache[candidate_id])
            exists_value = self._concept_exists_via_tool(candidate_id)
            if exists_cache is not None:
                exists_cache[candidate_id] = exists_value
            return exists_value

        if _exists(concept_id):
            if resolution_cache is not None:
                resolution_cache[concept_id] = concept_id
            return concept_id

        for candidate_name in self._candidate_names_from_concept_id(concept_id):
            resolved = self._resolve_concept_id_via_tool(
                candidate_name,
                preferred_language=preferred_language,
            )
            if isinstance(resolved, str) and _exists(resolved):
                if resolution_cache is not None:
                    resolution_cache[concept_id] = resolved
                return resolved

        if resolution_cache is not None:
            resolution_cache[concept_id] = None
        return None

    def _is_write_tool(
        self,
        tool_name: str,
        method_catalogue: Mapping[str, Any],
    ) -> bool:
        metadata = method_catalogue.get(tool_name)
        if isinstance(metadata, Mapping):
            category = metadata.get("category")
            if isinstance(category, str) and category.strip().lower() == "write":
                return True
        return tool_name in self._WRITE_TOOL_CONCEPT_ID_FIELDS

    def _rewrite_write_payload_concept_ids(
        self,
        *,
        tool_name: str,
        payload: MutableMapping[str, Any],
        method_catalogue: Mapping[str, Any],
        warnings: list[str],
        preferred_language: str | None = None,
        exists_cache: MutableMapping[str, bool] | None = None,
        resolution_cache: MutableMapping[str, str | None] | None = None,
    ) -> None:
        if not self._is_write_tool(tool_name, method_catalogue):
            return

        concept_fields = self._WRITE_TOOL_CONCEPT_ID_FIELDS.get(tool_name, ())
        if not concept_fields:
            return

        for field_name in concept_fields:
            raw_value = payload.get(field_name)
            if not self._looks_like_concept_id(raw_value):
                continue
            raw_concept_id = cast(str, raw_value)

            resolved = self._resolve_missing_concept_id(
                raw_concept_id,
                preferred_language=preferred_language,
                exists_cache=exists_cache,
                resolution_cache=resolution_cache,
            )
            if isinstance(resolved, str) and resolved != raw_concept_id:
                payload[field_name] = resolved
                warnings.append(
                    f"{tool_name}: resolved {field_name} from {raw_concept_id} to {resolved}"
                )
            elif resolved is None:
                warnings.append(
                    f"{tool_name}: unresolved concept ID in {field_name}: {raw_concept_id}"
                )

    @staticmethod
    def _extract_add_relationship_missing_concept_id(
        result_payload: Mapping[str, Any],
    ) -> str | None:
        error_details = result_payload.get("error_details")
        if not isinstance(error_details, Mapping):
            return None

        nested = error_details.get("details")
        if isinstance(nested, Mapping):
            nested_concept_id = nested.get("concept_id")
            if isinstance(nested_concept_id, str):
                return nested_concept_id

        for key in ("target", "source_id", "predicate"):
            value = error_details.get(key)
            if isinstance(value, str):
                return value
        return None

    def _retry_add_relationship_on_target_not_found(
        self,
        *,
        payload: MutableMapping[str, Any],
        result_payload: Mapping[str, Any],
        preferred_language: str | None = None,
    ) -> tuple[MutableMapping[str, Any] | None, Mapping[str, Any] | None]:
        error_code = result_payload.get("error_code") or result_payload.get("error")
        if (
            not isinstance(error_code, str)
            or error_code.strip().lower() != "target_not_found"
        ):
            return None, None

        missing_concept_id = self._extract_add_relationship_missing_concept_id(
            result_payload
        )
        candidate_fields = ("target", "source_id", "predicate")
        exists_cache: dict[str, bool] = {}
        resolution_cache: dict[str, str | None] = {}

        for field_name in candidate_fields:
            current_value = payload.get(field_name)
            if not self._looks_like_concept_id(current_value):
                continue
            current_concept_id = cast(str, current_value)
            if (
                isinstance(missing_concept_id, str)
                and missing_concept_id != current_concept_id
            ):
                continue

            resolved = self._resolve_missing_concept_id(
                current_concept_id,
                preferred_language=preferred_language,
                exists_cache=exists_cache,
                resolution_cache=resolution_cache,
            )
            if not isinstance(resolved, str) or resolved == current_concept_id:
                continue

            retry_payload = dict(payload)
            retry_payload[field_name] = resolved
            return retry_payload, {
                "reason": "target_not_found",
                "field": field_name,
                "from": current_concept_id,
                "to": resolved,
            }

        return None, None

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
        emit_progress: Callable[[Mapping[str, Any]], None] | None = None,
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

            if callable(emit_progress):
                emit_progress(
                    {
                        "status": "llm_call_start",
                        "stage": stage,
                        "model": model_name,
                        "candidate": (
                            dict(telemetry) if isinstance(telemetry, Mapping) else None
                        ),
                    }
                )
            llm_start = time.perf_counter()
            try:
                response = client.generate(
                    prompt,
                    context=cast(Optional[List[Dict[str, Any]]], context),
                    model=model_name,
                )
                duration_ms = (time.perf_counter() - llm_start) * 1000.0
                if callable(emit_progress):
                    emit_progress(
                        {
                            "status": "llm_call_chunk",
                            "stage": stage,
                            "model": model_name,
                            "chunks": 1,
                            "duration_ms": int(duration_ms),
                        }
                    )
                    emit_progress(
                        {
                            "status": "llm_call_end",
                            "stage": stage,
                            "model": model_name,
                            "duration_ms": int(duration_ms),
                            "success": True,
                        }
                    )
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
                if callable(emit_progress):
                    emit_progress(
                        {
                            "status": "llm_call_end",
                            "stage": stage,
                            "model": model_name,
                            "duration_ms": int(duration_ms),
                            "success": False,
                            "error": str(exc),
                        }
                    )
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
        emit_progress: Callable[[Mapping[str, Any]], None] | None = None,
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

            if callable(emit_progress):
                emit_progress(
                    {
                        "status": "llm_call_start",
                        "stage": stage,
                        "model": model_name,
                        "candidate": (
                            dict(telemetry) if isinstance(telemetry, Mapping) else None
                        ),
                    }
                )
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
                if callable(emit_progress):
                    completion_tokens = None
                    if isinstance(getattr(llm_response, "usage", None), Mapping):
                        raw_completion_tokens = llm_response.usage.get(
                            "completion_tokens"
                        )
                        if isinstance(raw_completion_tokens, int):
                            completion_tokens = raw_completion_tokens
                    emit_progress(
                        {
                            "status": "llm_call_chunk",
                            "stage": stage,
                            "model": model_name,
                            "chunks": 1,
                            "tokens_streamed": completion_tokens,
                            "duration_ms": int(duration_ms),
                        }
                    )
                    emit_progress(
                        {
                            "status": "llm_call_end",
                            "stage": stage,
                            "model": model_name,
                            "duration_ms": int(duration_ms),
                            "success": True,
                        }
                    )
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
                if callable(emit_progress):
                    emit_progress(
                        {
                            "status": "llm_call_end",
                            "stage": stage,
                            "model": model_name,
                            "duration_ms": int(duration_ms),
                            "success": False,
                            "error": str(exc),
                        }
                    )
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
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    if isinstance(item.get("recipient_name"), str) and isinstance(
                        item.get("parameters"), dict
                    ):
                        return True
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

            tool_uses = parsed.get("tool_uses")
            has_tool_uses_shape = (
                isinstance(tool_uses, list)
                and bool(tool_uses)
                and isinstance(tool_uses[0], dict)
                and isinstance(tool_uses[0].get("recipient_name"), str)
                and isinstance(tool_uses[0].get("parameters"), dict)
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
                "tool_uses",
            }
            missing_action_but_tool_shape = (
                self._ACTION_FIELD not in parsed
                and has_tool
                and has_payload
                and set(parsed.keys()) <= allowed_tool_like_keys
            )

            return (
                has_action and has_tool and has_payload
            ) or missing_action_but_tool_shape or has_tool_uses_shape
        except (json.JSONDecodeError, TypeError):
            return False

    def _extract_tool_calls(self, text: str) -> list[_ToolCallRequest] | None:
        """Extract one or more tool-call objects from the model response.

        Accepts either a single tool-call JSON object or a JSON array of tool-call
        objects. Returns None if the response is not a pure tool call.

        Also accepts a compatibility envelope used by some agent hosts:
        ``{"tool_uses":[{"recipient_name":"functions.tool","parameters":{...}}]}``.

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

        method_catalogue: Mapping[str, Any] = {}
        describe_methods = getattr(self._gateway, "describe_methods", None)
        if callable(describe_methods):
            try:
                described = describe_methods()
                if isinstance(described, Mapping):
                    method_catalogue = described
            except Exception:
                method_catalogue = {}
        available_tools = {
            name for name in method_catalogue.keys() if isinstance(name, str)
        }

        structural_predicate_aliases = {
            "instance_of",
            "instanceOf",
            "type_of",
            "typeOf",
            "subtype",
            "instance",
            "is_a_type_of",
            "is_an_instance_of",
            "has_subtype",
            "has_instance",
        }
        concept_like_fields = {
            "source_id",
            "target",
            "target_id",
            "concept_id",
            "parent_id",
            "assignee_concept_id",
            "creator_concept_id",
            "organisation_concept_id",
            "task_concept_id",
            "predicate",
        }
        for fields in self._WRITE_TOOL_CONCEPT_ID_FIELDS.values():
            concept_like_fields.update(fields)

        def _normalise_tool_name(raw_name: Any) -> str | None:
            if not isinstance(raw_name, str):
                return None
            cleaned = raw_name.strip()
            if not cleaned:
                return None

            candidates: list[str] = [cleaned]
            if cleaned.startswith("functions."):
                candidates.append(cleaned[len("functions.") :].strip())
            if cleaned.startswith("functions/"):
                candidates.append(cleaned[len("functions/") :].strip())
            if cleaned.startswith("tools."):
                candidates.append(cleaned[len("tools.") :].strip())

            for candidate in list(candidates):
                if candidate.startswith("mcp__") and "__" in candidate:
                    suffix = candidate.split("__")[-1].strip()
                    if suffix:
                        candidates.append(suffix)
                if "." in candidate:
                    tail = candidate.rsplit(".", 1)[-1].strip()
                    if tail:
                        candidates.append(tail)

            ordered: list[str] = []
            for candidate in candidates:
                if candidate and candidate not in ordered:
                    ordered.append(candidate)

            if available_tools:
                for candidate in ordered:
                    if candidate in available_tools:
                        return candidate

            for candidate in ordered:
                if candidate != cleaned:
                    return candidate
            return cleaned

        def _clean_payload_scalar(value: str, *, field_name: str | None) -> str:
            cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not cleaned:
                return cleaned

            lines = [line.strip() for line in cleaned.split("\n") if line.strip()]
            if len(lines) >= 2 and lines[-1].lower() in {
                "individual",
                "predicate",
                "type",
                "instance",
            }:
                lines = lines[:-1]

            if lines:
                cleaned = "\n".join(lines).strip()

            cleaned = re.sub(
                r"\s+(?:individual|predicate|type|instance)\s*$",
                "",
                cleaned,
                flags=re.IGNORECASE,
            ).strip()

            if (
                isinstance(field_name, str)
                and field_name in concept_like_fields
                and cleaned
                and not cleaned.startswith("#V#")
                and bool(re.match(r"^[A-Za-z][A-Za-z0-9._-]*$", cleaned))
            ):
                if field_name == "predicate" and cleaned in structural_predicate_aliases:
                    return cleaned
                return f"#V#{cleaned}"

            return cleaned

        def _clean_payload_value(value: Any, *, field_name: str | None = None) -> Any:
            if isinstance(value, str):
                return _clean_payload_scalar(value, field_name=field_name)
            if isinstance(value, MutableMapping):
                cleaned_map: dict[str, Any] = {}
                for key, nested_value in value.items():
                    key_name = key if isinstance(key, str) else None
                    cleaned_map[str(key)] = _clean_payload_value(
                        nested_value, field_name=key_name
                    )
                return cleaned_map
            if isinstance(value, list):
                return [
                    _clean_payload_value(item, field_name=field_name) for item in value
                ]
            return value

        def _extract_tool_uses_batch(raw_text: str) -> list[Any] | None:
            marker = '"tool_uses"'
            marker_index = raw_text.find(marker)
            if marker_index == -1:
                return None

            array_start = raw_text.find("[", marker_index)
            if array_start == -1:
                return None

            depth = 0
            in_string = False
            escaped = False

            for index in range(array_start, len(raw_text)):
                char = raw_text[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue

                if char == '"':
                    in_string = True
                    continue
                if char == "[":
                    depth += 1
                    continue
                if char == "]":
                    depth -= 1
                    if depth == 0:
                        array_text = raw_text[array_start : index + 1]
                        try:
                            parsed_array = json.loads(array_text)
                        except json.JSONDecodeError:
                            repaired = self._repair_truncated_json(array_text)
                            if repaired is None:
                                return None
                            try:
                                parsed_array = json.loads(repaired)
                            except json.JSONDecodeError:
                                return None
                        return parsed_array if isinstance(parsed_array, list) else None

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
                '"tool_uses"',
                '"recipient_name"',
                '"parameters"',
            )
        )

        decoder = json.JSONDecoder()
        tool_uses_batch = _extract_tool_uses_batch(raw)
        raw_trailing = ""
        trailing = ""

        if tool_uses_batch is not None:
            parsed: Any = {"tool_uses": tool_uses_batch}
        else:
            if not (raw.startswith("{") or raw.startswith("[")):
                return None

            # Avoid raising parse errors for non-tool JSON (e.g., when the model
            # returns a JSON answer or the user pasted JSON). Only attempt to parse
            # tool calls when the response looks tool-shaped.
            if not looks_like_tool_call:
                return None

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
                for key in ("name", "recipient_name", "recipient", "method"):
                    alt_name = candidate.get(key)
                    if isinstance(alt_name, str) and alt_name.strip():
                        tool_name = alt_name.strip()
                        break
            tool_name = _normalise_tool_name(tool_name)
            payload_value = candidate.get(self._PAYLOAD_FIELD)
            action_value = candidate.get(self._ACTION_FIELD)
            if not isinstance(payload_value, MutableMapping):
                for alt_key in ("params", "parameters", "arguments", "args"):
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

            use_compat_cleaning = any(
                key in candidate
                for key in ("recipient_name", "parameters", "params", "arguments", "args")
            )

            if has_payload and use_compat_cleaning:
                payload_value = cast(
                    MutableMapping[str, Any],
                    _clean_payload_value(payload_value, field_name=None),
                )

            missing_action = self._ACTION_FIELD not in candidate
            strict_shape = set(candidate.keys()) <= {
                self._TOOL_FIELD,
                self._PAYLOAD_FIELD,
                "params",
                "arguments",
                "args",
                "name",
                "recipient_name",
                "recipient",
                "method",
                "parameters",
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
            if not isinstance(wrapped_calls, list):
                wrapped_calls = parsed.get("tool_uses")
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
        conversation_session_id: str | None = None,
        turn_id: str | None = None,
    ) -> _ToolCallPreflightResult:
        errors: list[str] = []
        warnings: list[str] = []
        tool_unavailable: list[str] = []

        if not tool_calls:
            return _ToolCallPreflightResult(None, errors, warnings, tool_unavailable)

        available_tools = set(method_catalogue.keys())
        enforce_availability = bool(available_tools)
        exists_cache: dict[str, bool] = {}
        resolution_cache: dict[str, str | None] = {}

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
                conversation_session_id=conversation_session_id,
                turn_id=turn_id,
            )
            # Deterministically resolve close-but-invalid concept IDs for
            # write tools before schema validation/execution.
            self._rewrite_write_payload_concept_ids(
                tool_name=tool_name,
                payload=payload,
                method_catalogue=method_catalogue,
                warnings=warnings,
                exists_cache=exists_cache,
                resolution_cache=resolution_cache,
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
        conversation_session_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        if tool_name.startswith("gmail_"):
            if not payload.get("profile") and selected_gmail_profile:
                payload["profile"] = selected_gmail_profile
            if "namespace" in payload:
                payload.pop("namespace", None)
            return

        if user_namespace and "namespace" not in payload:
            payload["namespace"] = user_namespace

        # Preserve user-attribution for auto-created concepts so namespace
        # isolation has a deterministic provenance trail (JVNAUTOSCI-925).
        if (
            tool_name == "create_concepts"
            and "created_by_concept_id" not in payload
            and user_namespace
        ):
            actor_concept_id = self._derive_actor_concept_id_from_namespace(
                user_namespace
            )
            if actor_concept_id:
                payload["created_by_concept_id"] = actor_concept_id

        # Attach lightweight provenance for auto text writes so generated
        # content remains attributable in text_value provenance fields.
        if tool_name in {"upsert_text_relation", "upsert_singleton_text_relation"}:
            provenance_payload = payload.get("provenance")
            provenance: dict[str, Any] = (
                dict(provenance_payload)
                if isinstance(provenance_payload, Mapping)
                else {}
            )
            provenance.setdefault("source", "internal_mcp_orchestrator")
            provenance.setdefault("path", "auto_extension")
            if isinstance(user_namespace, str) and user_namespace.strip():
                namespace = user_namespace.strip()
                provenance.setdefault("namespace", namespace)
                actor_concept_id = self._derive_actor_concept_id_from_namespace(
                    namespace
                )
                if actor_concept_id:
                    provenance.setdefault("actor_concept_id", actor_concept_id)
            if isinstance(conversation_session_id, str) and conversation_session_id:
                provenance.setdefault("conversation_session_id", conversation_session_id)
            if isinstance(turn_id, str) and turn_id:
                provenance.setdefault("turn_id", turn_id)
            if provenance:
                payload["provenance"] = provenance

        # JVNAUTOSCI-1040: Inject conversation session ID for task_create
        if tool_name == "task_create" and conversation_session_id:
            if "originating_session_id" not in payload and "session_id" not in payload:
                payload["originating_session_id"] = conversation_session_id

    @staticmethod
    def _derive_actor_concept_id_from_namespace(namespace: str | None) -> str | None:
        if not isinstance(namespace, str):
            return None
        cleaned = namespace.strip()
        if not cleaned or not cleaned.startswith("#V#"):
            return None
        if "@" not in cleaned:
            return cleaned
        user_part = cleaned.split("@", 1)[0].strip()
        return user_part or None

    @staticmethod
    def _is_high_impact_review_mode_enabled() -> bool:
        try:
            from src.backend.services.settings_service import (
                get_require_human_review_for_high_impact_kb_writes,
            )

            return bool(get_require_human_review_for_high_impact_kb_writes())
        except Exception:
            return False

    def _evaluate_high_impact_write_guard(
        self,
        *,
        tool_name: str,
        prompt: str,
        recent_user_prompts: list[str],
        user_namespace: str | None,
    ) -> str | None:
        if not self._is_high_impact_review_mode_enabled():
            return None
        if not is_high_impact_vontology_write_tool(tool_name):
            return None
        if not (isinstance(user_namespace, str) and user_namespace.strip()):
            return self._HIGH_IMPACT_NAMESPACE_REASON
        approved = prompt_grants_high_impact_kb_write_approval(
            prompt=prompt,
            recent_user_prompts=recent_user_prompts,
        )
        if not approved:
            return self._HIGH_IMPACT_REVIEW_REASON
        return None

    def _build_blocked_write_message(
        self,
        *,
        tool_name: str,
        reason: str | None,
    ) -> str:
        if reason == self._HIGH_IMPACT_NAMESPACE_REASON:
            return (
                f"Blocked high-impact Vontology write tool {tool_name!r}: "
                "an authenticated namespace is required to preserve namespace isolation."
            )
        if reason == self._HIGH_IMPACT_REVIEW_REASON:
            return (
                f"Blocked high-impact Vontology write tool {tool_name!r}: "
                "human review approval is required. Include explicit approval wording "
                "for the Vontology mutation and retry."
            )
        return (
            f"Blocked write tool {tool_name!r}: the user request appears read-only. "
            "If you intended to perform a write, restate the request explicitly."
        )

    def _build_write_interaction_metadata(
        self,
        *,
        tool_name: str,
        user_namespace: str | None,
        conversation_session_id: str | None,
        turn_id: str | None,
        guard_reason: str | None = None,
    ) -> dict[str, Any]:
        actor_concept_id = self._derive_actor_concept_id_from_namespace(user_namespace)
        metadata: dict[str, Any] = {
            "type": "knowledge_interaction",
            "tool": tool_name,
            "namespace": user_namespace,
            "actor_concept_id": actor_concept_id,
            "conversation_session_id": conversation_session_id,
            "turn_id": turn_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
        if guard_reason:
            metadata["guard_reason"] = guard_reason
        return metadata

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
    def _extract_create_concept_result_label(result: Mapping[str, Any]) -> str | None:
        """Build a stable display label for one create_concepts result item.

        Prefer showing both requested name and the resulting canonical concept ID
        so users can immediately see exactly which concept was created.
        """

        if not isinstance(result, Mapping):
            return None

        requested_name_raw = (
            result.get("requested_name")
            or result.get("input_name")
            or result.get("name")
        )
        requested_name = (
            str(requested_name_raw).strip()
            if isinstance(requested_name_raw, str) and requested_name_raw.strip()
            else None
        )

        concept_id_value: str | None = None
        for key in ("concept_id", "canonical_concept_id", "existing_concept_id"):
            raw = result.get(key)
            if isinstance(raw, str) and raw.strip():
                concept_id_value = raw.strip()
                break
        if concept_id_value is None:
            nested_concept = result.get("concept")
            if isinstance(nested_concept, Mapping):
                nested_id = nested_concept.get("concept_id")
                if isinstance(nested_id, str) and nested_id.strip():
                    concept_id_value = nested_id.strip()

        if requested_name and concept_id_value:
            return f"{requested_name} ({concept_id_value})"
        if concept_id_value:
            return concept_id_value
        if requested_name:
            return requested_name
        return None

    @staticmethod
    def _extract_result_summary(
        tool_name: str,
        payload: Any,
        *,
        max_length: int = 80,
    ) -> str | None:
        """Extract a human-readable summary from a tool result for progress display.

        Returns a short string describing what the tool actually did, or None if
        no meaningful summary can be extracted.

        Tool salience categories:
        - HIGH: create_concepts, search_concepts, fetch_concept, add_relationship,
                remove_relationship, add_names, merge_concepts, delete_concept,
                create_task, update_task_status
        - MEDIUM: search_*, find_*, get_task, gmail_*, upsert_text_relation
        - LOW: get_context, audit_*, get_*_status, internal tools
        """
        if payload is None:
            return None
        if not isinstance(payload, dict):
            # For lists, summarise count
            if isinstance(payload, list):
                count = len(payload)
                return f"{count} item{'s' if count != 1 else ''}" if count else None
            return None

        # Error responses
        if payload.get("error"):
            error_msg = payload.get("error", "")
            error_code = payload.get("error_code")
            if error_code:
                return f"Error: {error_code}"
            if isinstance(error_msg, str) and error_msg:
                short = error_msg[:50] + "..." if len(error_msg) > 50 else error_msg
                return f"Error: {short}"
            return "Error"

        # === VONTOLOGY-DRIVEN TEMPLATE (checked first with cache) ===
        # Try applying display template from Vontology tool metadata
        try:
            metadata = get_tool_metadata(tool_name)
            if metadata.display_template:
                vontology_result = (
                    InternalMCPChatOrchestrator._apply_vontology_template(
                        metadata.display_template,
                        payload,
                        max_length=max_length,
                    )
                )
                if vontology_result:
                    return vontology_result
        except Exception:
            # Fall back to hardcoded patterns if metadata service fails
            pass

        # === TOOL-SPECIFIC HANDLERS ===
        tool_lower = tool_name.lower()

        # --- Relationship tools (HIGH salience) ---
        if tool_lower == "add_relationship":
            predicate = payload.get("predicate") or payload.get("predicate_id")
            target = payload.get("target_id") or payload.get("target_concept_id")
            if predicate and target:
                pred_short = str(predicate).replace("#V#", "")[:20]
                tgt_short = str(target).replace("#V#", "")[:20]
                return f"{pred_short} → {tgt_short}"
            if payload.get("success"):
                return "Relationship added"
            return None

        if tool_lower == "remove_relationship":
            predicate = payload.get("predicate") or payload.get("predicate_id")
            target = payload.get("target_id") or payload.get("target_concept_id")
            if predicate and target:
                pred_short = str(predicate).replace("#V#", "")[:20]
                tgt_short = str(target).replace("#V#", "")[:20]
                return f"Removed: {pred_short} → {tgt_short}"
            if payload.get("success") or payload.get("removed"):
                return "Relationship removed"
            return None

        # --- Name management (HIGH salience) ---
        if tool_lower == "add_names":
            added = payload.get("added") or payload.get("names_added")
            if isinstance(added, list) and added:
                names = [str(n.get("text") or n) for n in added[:3] if n]
                summary = ", ".join(names)
                if len(added) > 3:
                    summary += f" (+{len(added) - 3})"
                return f"Added: {summary}"
            if isinstance(added, int) and added > 0:
                return f"Added {added} name{'s' if added != 1 else ''}"
            if payload.get("success"):
                return "Names added"
            return None

        # --- Merge/delete (HIGH salience) ---
        if tool_lower == "merge_concepts":
            source = payload.get("source_id") or payload.get("merged_from")
            target = payload.get("target_id") or payload.get("merged_into")
            if source and target:
                src_short = str(source).replace("#V#", "")[:15]
                tgt_short = str(target).replace("#V#", "")[:15]
                return f"Merged {src_short} → {tgt_short}"
            if payload.get("success"):
                return "Concepts merged"
            return None

        if tool_lower == "delete_concept":
            deleted = payload.get("concept_id") or payload.get("deleted")
            if deleted:
                del_short = str(deleted).replace("#V#", "")[:25]
                return f"Deleted: {del_short}"
            if payload.get("success"):
                return "Concept deleted"
            return None

        # --- Task tools (HIGH salience) ---
        if tool_lower in ("create_task", "task_create"):
            title = payload.get("title") or payload.get("task_title")
            task_id = payload.get("task_id") or payload.get("id")
            if title:
                return f"Created: {str(title)[:40]}"
            if task_id:
                return f"Created task: {task_id}"
            return "Task created"

        if tool_lower in ("update_task_status", "task_update_status"):
            new_status = payload.get("status") or payload.get("new_status")
            task_id = payload.get("task_id")
            if new_status:
                return f"Status → {new_status}"
            if payload.get("success"):
                return "Status updated"
            return None

        if tool_lower in ("get_task", "task_get"):
            title = payload.get("title") or payload.get("task_title")
            status = payload.get("status")
            if title:
                suffix = f" ({status})" if status else ""
                return f"{str(title)[:35]}{suffix}"
            return None

        if tool_lower in ("list_my_tasks", "task_list"):
            tasks = payload.get("tasks") or payload.get("results") or []
            if isinstance(tasks, list):
                return f"{len(tasks)} task{'s' if len(tasks) != 1 else ''}"
            return None

        if tool_lower in ("assign_task", "task_assign"):
            assignee = payload.get("assignee") or payload.get("assigned_to")
            if assignee:
                return f"Assigned to: {str(assignee)[:25]}"
            if payload.get("success"):
                return "Task assigned"
            return None

        # --- Text relation tools (MEDIUM salience) ---
        if tool_lower in ("upsert_text_relation", "upsert_singleton_text_relation"):
            predicate = payload.get("predicate")
            created = payload.get("created")
            updated = payload.get("updated")
            if predicate:
                pred_short = str(predicate).replace("has", "").replace("Has", "")[:20]
                action = "Created" if created else "Updated" if updated else "Set"
                return f"{action}: {pred_short}"
            if created or updated or payload.get("success"):
                return "Created" if created else "Updated" if updated else "Success"
            return None

        if tool_lower == "delete_text_relation":
            relation_id = payload.get("relation_id") or payload.get("id")
            if relation_id:
                return f"Deleted relation: {str(relation_id)[:20]}"
            if payload.get("success") or payload.get("deleted"):
                return "Relation deleted"
            return None

        if tool_lower == "get_text_relations":
            relations = payload.get("relations") or payload.get("text_relations") or []
            if isinstance(relations, list):
                return f"{len(relations)} relation{'s' if len(relations) != 1 else ''}"
            return None

        # --- RAG sync (MEDIUM salience) ---
        if tool_lower == "rag_sync_text_relations":
            synced = payload.get("synced") or payload.get("indexed")
            errors = payload.get("errors") or payload.get("failed")
            if isinstance(synced, int):
                result = f"Synced {synced}"
                if errors:
                    result += f", {errors} error{'s' if errors != 1 else ''}"
                return result
            if payload.get("success"):
                return "Sync complete"
            return None

        # --- Search tools (MEDIUM salience) ---
        if tool_lower == "search_arxiv":
            papers = payload.get("papers") or payload.get("results") or []
            if isinstance(papers, list):
                count = len(papers)
                if count == 0:
                    return "No papers found"
                titles = [
                    str(p.get("title", ""))[:25] for p in papers[:2] if p.get("title")
                ]
                if titles:
                    summary = ", ".join(titles)
                    if count > 2:
                        summary += f" (+{count - 2})"
                    return f"Found: {summary}"
                return f"{count} paper{'s' if count != 1 else ''}"
            return None

        if tool_lower == "search_web":
            results = payload.get("results") or payload.get("items") or []
            if isinstance(results, list):
                count = len(results)
                if count == 0:
                    return "No results"
                return f"{count} web result{'s' if count != 1 else ''}"
            return None

        if tool_lower == "search_knowledge_base":
            results = payload.get("results") or payload.get("items") or []
            if isinstance(results, list):
                count = len(results)
                if count == 0:
                    return "No matches"
                return f"{count} KB match{'es' if count != 1 else ''}"
            return None

        if tool_lower in ("context_search", "qna_search"):
            results = payload.get("results") or payload.get("items") or []
            answer = payload.get("answer")
            if answer:
                return (
                    f"Answer: {str(answer)[:50]}..."
                    if len(str(answer)) > 50
                    else f"Answer: {answer}"
                )
            if isinstance(results, list):
                return f"{len(results)} result{'s' if len(results) != 1 else ''}"
            return None

        # --- Gmail tools (MEDIUM salience) ---
        if tool_lower == "gmail_list_messages":
            messages = payload.get("messages") or payload.get("results") or []
            if isinstance(messages, list):
                return f"{len(messages)} message{'s' if len(messages) != 1 else ''}"
            return None

        if tool_lower == "gmail_get_message":
            subject = payload.get("subject") or payload.get("headers", {}).get(
                "Subject"
            )
            if subject:
                return f"Message: {str(subject)[:40]}"
            return "Message retrieved"

        if tool_lower == "gmail_get_attachment":
            filename = payload.get("filename") or payload.get("name")
            if filename:
                return f"Attachment: {str(filename)[:30]}"
            return "Attachment retrieved"

        # --- Paper tools (MEDIUM salience) ---
        if tool_lower == "get_paper_metadata":
            title = payload.get("title")
            if title:
                return f"Paper: {str(title)[:40]}"
            return None

        if tool_lower == "download_paper":
            arxiv_id = payload.get("arxiv_id") or payload.get("paper_id")
            if arxiv_id:
                return f"Downloaded: {arxiv_id}"
            if payload.get("success"):
                return "Paper downloaded"
            return None

        # --- Find tools (MEDIUM salience) ---
        if tool_lower == "find_subconcepts":
            children = payload.get("children") or payload.get("subconcepts") or []
            if isinstance(children, list):
                count = len(children)
                if count == 0:
                    return "No children"
                names = [
                    str(c.get("name", c.get("concept_id", "")))[:20]
                    for c in children[:3]
                ]
                if names:
                    summary = ", ".join(names)
                    if count > 3:
                        summary += f" (+{count - 3})"
                    return f"Children: {summary}"
                return f"{count} child{'ren' if count != 1 else ''}"
            return None

        if tool_lower == "find_concepts_by_name":
            matches = (
                payload.get("matches")
                or payload.get("concepts")
                or payload.get("results")
                or []
            )
            if isinstance(matches, list):
                count = len(matches)
                if count == 0:
                    return "No matches"
                names = [str(m.get("name", m.get("id", "")))[:20] for m in matches[:3]]
                if names:
                    summary = ", ".join(names)
                    if count > 3:
                        summary += f" (+{count - 3})"
                    return f"Found: {summary}"
                return f"{count} match{'es' if count != 1 else ''}"
            return None

        if tool_lower == "resolve_concept_by_name":
            concept_id = payload.get("concept_id")
            name = payload.get("name")
            if name:
                return f"Resolved: {str(name)[:30]}"
            if concept_id:
                cid_short = str(concept_id).replace("#V#", "")[:25]
                return f"Resolved: {cid_short}"
            if payload.get("not_found") or not payload.get("concept_id"):
                return "Not found"
            return None

        # --- Update concept (MEDIUM salience) ---
        if tool_lower == "update_concept":
            concept_id = payload.get("concept_id")
            if concept_id:
                cid_short = str(concept_id).replace("#V#", "")[:25]
                return f"Updated: {cid_short}"
            if payload.get("success"):
                return "Concept updated"
            return None

        # === GENERIC PATTERNS (fallback) ===

        # create_concepts: show created concept names
        if "results" in payload and "successful" in payload:
            results = payload.get("results", [])
            successful = payload.get("successful", 0)
            names = []
            for r in results:
                if isinstance(r, dict) and r.get("success"):
                    name = InternalMCPChatOrchestrator._extract_create_concept_result_label(
                        r
                    )
                    if name:
                        names.append(str(name))
            if names:
                names_str = ", ".join(names[:3])
                if len(names) > 3:
                    names_str += f" (+{len(names) - 3} more)"
                return f"Created: {names_str}"
            elif successful:
                return f"Created {successful} concept{'s' if successful != 1 else ''}"
            return None

        # search_concepts: show result count and sample names
        if "concepts" in payload or (
            "results" in payload and "successful" not in payload
        ):
            items = payload.get("concepts") or payload.get("results") or []
            if isinstance(items, list):
                count = len(items)
                if count == 0:
                    return "No results"
                names = []
                for item in items[:3]:
                    if isinstance(item, dict):
                        name = (
                            item.get("name")
                            or item.get("text")
                            or item.get("concept_id")
                            or item.get("title")
                        )
                        if name:
                            names.append(str(name)[:30])
                if names:
                    summary = ", ".join(names)
                    if count > 3:
                        summary += f" (+{count - 3} more)"
                    return f"Found: {summary}"
                return f"Found {count} result{'s' if count != 1 else ''}"

        # Single concept fetch: show concept name
        if "concept_id" in payload and "success" not in payload:
            name = payload.get("name")
            concept_id = payload.get("concept_id")
            if name:
                return f"Loaded: {name}"
            elif concept_id:
                cid_short = str(concept_id).replace("#V#", "")[:25]
                return f"Loaded: {cid_short}"

        # Generic success with target
        if payload.get("success") or payload.get("created") or payload.get("updated"):
            relation_id = payload.get("relation_id") or payload.get("id")
            target = payload.get("concept_id") or payload.get("target_concept_id")
            if relation_id:
                return f"Created relation: {str(relation_id)[:20]}"
            if target:
                tgt_short = str(target).replace("#V#", "")[:25]
                return f"Updated: {tgt_short}"
            return "Success"

        # Count-based results
        if "count" in payload:
            count = payload.get("count")
            if isinstance(count, int):
                return f"{count} item{'s' if count != 1 else ''}"

        # Tree/hierarchy results
        if "tree" in payload or "children" in payload:
            items = payload.get("tree") or payload.get("children") or []
            if isinstance(items, list):
                return f"{len(items)} node{'s' if len(items) != 1 else ''}"

        # Message-based responses
        if "message" in payload:
            msg = str(payload["message"])
            if len(msg) <= max_length:
                return msg
            return msg[: max_length - 3] + "..."

        return None

    @staticmethod
    def _apply_vontology_template(
        template: str,
        payload: dict[str, Any],
        max_length: int = 80,
    ) -> str | None:
        """Apply a Vontology display template to a tool payload.

        Templates use {placeholder} syntax. Supported placeholders are extracted
        from the payload using common field name patterns.

        Returns None if the template cannot be meaningfully applied.
        """
        if not template:
            return None

        def _short_concept_id(value: Any, *, max_chars: int = 25) -> str | None:
            if not isinstance(value, str):
                return None
            cleaned = value.strip()
            if not cleaned:
                return None
            return cleaned.replace("#V#", "")[:max_chars]

        def _build_search_query_label(data: dict[str, Any], *, limit: int = 50) -> str:
            """Build a human-meaningful search descriptor for status text.

            This avoids low-signal summaries like `for ""` when search_concepts
            intentionally uses an empty query with filters (for example instance_of).
            """

            query_info = data.get("query_info")
            info = query_info if isinstance(query_info, dict) else {}

            raw_query = data.get("query")
            if not isinstance(raw_query, str):
                raw_query = info.get("query")

            if isinstance(raw_query, str) and raw_query.strip():
                text = raw_query.strip()
                return text[:limit] + "..." if len(text) > limit else text

            parts: list[str] = []

            instance_of = data.get("instance_of")
            if not isinstance(instance_of, str):
                instance_of = info.get("instance_of")
            instance_short = _short_concept_id(instance_of)
            if instance_short:
                parts.append(f"instances of {instance_short}")

            filter_kind = data.get("filter_kind")
            if not isinstance(filter_kind, list):
                filter_kind = info.get("filter_kind")
            if isinstance(filter_kind, list):
                kinds = [str(kind).strip() for kind in filter_kind if str(kind).strip()]
                if kinds:
                    kind_text = "/".join(kinds[:3])
                    if len(kinds) > 3:
                        kind_text += "+..."
                    parts.append(f"kind={kind_text}")

            scope_root = data.get("scope_root")
            if not isinstance(scope_root, str):
                scope_root = info.get("scope_root")
            scope_short = _short_concept_id(scope_root)
            if scope_short:
                parts.append(f"scope={scope_short}")

            if parts:
                label = "; ".join(parts)
                return label[:limit] + "..." if len(label) > limit else label

            return "all concepts"

        # Build a context dict from the payload with common field mappings
        context: dict[str, Any] = {}

        # Count-related fields
        for key in ("count", "total", "successful", "added_count", "relations_found"):
            if key in payload:
                context["count"] = payload[key]
                break
        if "count" not in context:
            # Try to infer count from list fields
            for key in (
                "results",
                "concepts",
                "matches",
                "items",
                "tasks",
                "messages",
                "papers",
                "issues",
            ):
                if isinstance(payload.get(key), list):
                    context["count"] = len(payload[key])
                    break

        # Name/title fields
        for key in ("name", "title", "display_name", "summary"):
            if payload.get(key):
                context["name"] = str(payload[key])[:40]
                context["title"] = context["name"]
                break

        # Search query context (especially important for search_concepts status text)
        query_label = _build_search_query_label(payload)
        if query_label:
            context["query"] = query_label
            context["query_label"] = query_label

        # Names list (from create_concepts)
        if "results" in payload and isinstance(payload["results"], list):
            names = []
            for r in payload["results"]:
                if isinstance(r, dict) and r.get("success"):
                    n = InternalMCPChatOrchestrator._extract_create_concept_result_label(
                        r
                    )
                    if n:
                        names.append(str(n)[:48])
            if names:
                context["names"] = ", ".join(names[:3])
                if len(names) > 3:
                    context["names"] += f" (+{len(names) - 3})"

        # Concept IDs and relationship targets/sources
        for key in (
            "concept_id",
            "target_id",
            "target_concept_id",
            "target",
            "source_id",
            "source",
        ):
            if payload.get(key):
                val = str(payload[key]).replace("#V#", "")[:25]
                context.setdefault("concept_id", val)
                if "target" in key:
                    context["target"] = val
                elif "source" in key:
                    context["source"] = val

        # Relationship fields
        for key in ("predicate", "predicate_id"):
            if payload.get(key):
                context["predicate"] = str(payload[key]).replace("#V#", "")[:20]
                break

        # Jira fields
        for key in ("key", "issue_key"):
            if payload.get(key):
                context["key"] = str(payload[key])
                break
        if payload.get("status"):
            context["status"] = str(payload["status"])[:20]
        # Jira link fields
        if payload.get("inwardIssue") or payload.get("inward"):
            context["inward"] = str(payload.get("inwardIssue") or payload.get("inward"))
        if payload.get("outwardIssue") or payload.get("outward"):
            context["outward"] = str(
                payload.get("outwardIssue") or payload.get("outward")
            )

        # ArXiv fields
        if payload.get("arxiv_id"):
            context["arxiv_id"] = str(payload["arxiv_id"])

        # URL fields
        for key in ("url", "extracted_url", "source_url"):
            if payload.get(key):
                url = str(payload[key])
                context["url"] = url[:50] + "..." if len(url) > 50 else url
                break

        # Task fields
        if payload.get("task_id"):
            context["task_id"] = str(payload["task_id"])
        if payload.get("assignee") or payload.get("assigned_to"):
            context["assignee"] = str(
                payload.get("assignee") or payload.get("assigned_to")
            )[:25]

        # Message fields (Gmail)
        if payload.get("subject"):
            context["subject"] = str(payload["subject"])[:40]
        if payload.get("filename"):
            context["filename"] = str(payload["filename"])[:30]

        # Action fields (for upsert)
        if payload.get("created"):
            context["action"] = "Created"
        elif payload.get("updated"):
            context["action"] = "Updated"
        else:
            context["action"] = "Set"

        # Boolean exists
        if "exists" in payload:
            context["exists"] = "Yes" if payload["exists"] else "No"

        # Answer (QnA)
        if payload.get("answer"):
            ans = str(payload["answer"])
            context["answer"] = ans[:50] + "..." if len(ans) > 50 else ans

        # Try to apply the template
        try:
            # Find all placeholders
            import re

            placeholders = re.findall(r"\{(\w+)\}", template)
            if not placeholders:
                return template[:max_length]

            # Check if we have values for enough placeholders
            available = sum(1 for p in placeholders if p in context and context[p])
            if available < len(placeholders) * 0.5:
                # Less than half placeholders available, skip template
                return None

            # Substitute available values, leave others empty
            result = template
            for placeholder in placeholders:
                value = context.get(placeholder, "")
                result = result.replace(f"{{{placeholder}}}", str(value))

            # Clean up empty placeholders and extra spaces
            result = re.sub(r"\{[^}]*\}", "", result)
            result = re.sub(r"\s+", " ", result).strip()

            # Guard against low-signal empty-query rendering patterns.
            result = re.sub(r'for\s*""', 'for "all concepts"', result)
            result = re.sub(r"\bfor\s*$", "", result).strip()

            if not result or result == template:
                return None

            return result[:max_length] if len(result) > max_length else result

        except Exception:
            return None

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

    def _extract_topic_keywords_from_context(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]],
        *,
        max_recent_messages: int = 5,
    ) -> list[str]:
        """Extract significant topic keywords from recent conversation context.

        Used for context-based ontology vocabulary discovery (JVNAUTOSCI-1052).
        Combines keywords from the current prompt and recent user messages.
        """
        import re as _re

        texts: list[str] = []

        # Add current prompt
        if isinstance(prompt, str) and prompt.strip():
            texts.append(prompt.strip())

        # Extract recent user messages from context
        if context:
            user_messages: list[str] = []
            for msg in context:
                if not isinstance(msg, Mapping):
                    continue
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    user_messages.append(content.strip())
            # Take most recent messages (last N)
            for text in user_messages[-max_recent_messages:]:
                texts.append(text)

        if not texts:
            return []

        combined = " ".join(texts)

        # Remove explicit concept IDs to avoid circular discovery
        combined = _re.sub(r"#V#[A-Za-z0-9][A-Za-z0-9._-]*", "", combined)

        # Extract words (3+ chars, alphanumeric with underscores/hyphens)
        words = _re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{2,}\b", combined)

        # Common stop words to filter out
        stop_words = {
            "the",
            "and",
            "for",
            "are",
            "but",
            "not",
            "you",
            "all",
            "can",
            "had",
            "her",
            "was",
            "one",
            "our",
            "out",
            "has",
            "have",
            "been",
            "would",
            "could",
            "should",
            "will",
            "with",
            "this",
            "that",
            "from",
            "they",
            "what",
            "which",
            "when",
            "where",
            "there",
            "their",
            "about",
            "into",
            "than",
            "then",
            "some",
            "such",
            "only",
            "other",
            "also",
            "just",
            "like",
            "more",
            "most",
            "very",
            "much",
            "many",
            "how",
            "why",
            "who",
            "whom",
            "does",
            "did",
            "these",
            "those",
            "them",
            "being",
            "each",
            "few",
            "any",
            "both",
            "after",
            "before",
            "please",
            "help",
            "tell",
            "show",
            "give",
            "find",
            "make",
            "want",
            "need",
            "know",
            "think",
            "look",
            "use",
            "using",
            "used",
        }

        # Filter and deduplicate keywords
        seen: set[str] = set()
        keywords: list[str] = []
        for word in words:
            lower = word.lower()
            if lower in stop_words:
                continue
            if lower in seen:
                continue
            seen.add(lower)
            keywords.append(word)

        # Limit to most frequent/significant keywords (first 10)
        return keywords[:10]

    def _get_topic_vocabulary_cache_key(self, keywords: list[str]) -> str:
        """Generate a cache key from topic keywords."""
        if not keywords:
            return "__empty__"
        # Use sorted lowercase keywords for consistent hashing
        normalised = sorted(set(k.lower() for k in keywords if k))
        return ":".join(normalised[:6])

    def _discover_types_for_topic(
        self,
        keywords: list[str],
        preferred_language: str | None,
    ) -> list[dict[str, Any]]:
        """Discover types relevant to the given topic keywords.

        JVNAUTOSCI-1052: Query Vontology for types matching the conversation topic.
        """
        if not keywords:
            return []

        try:
            from src.backend.services.concept_search_service import search_concepts
        except Exception:
            return []

        query = " ".join(keywords[:6])
        try:
            result = search_concepts(
                query=query,
                filter_kind=["type"],
                match_type="similarity",
                min_similarity=0.50,
                limit=self._TOPIC_VOCABULARY_MAX_TYPES,
            )
        except Exception:
            return []

        entries = result.get("results") if isinstance(result, dict) else None
        if not isinstance(entries, list):
            return []

        types: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            concept_id = entry.get("concept_id")
            if not isinstance(concept_id, str) or not concept_id:
                continue
            if concept_id in seen:
                continue
            name = entry.get("name")
            score = entry.get("similarity_score")
            types.append(
                {
                    "concept_id": concept_id,
                    "name": (
                        self._normalise_preflight_display_name(name) if name else None
                    ),
                    "score": score,
                }
            )
            seen.add(concept_id)

        return types

    def _discover_predicates_for_topic(
        self,
        keywords: list[str],
        preferred_language: str | None,
    ) -> list[dict[str, Any]]:
        """Discover predicates relevant to the given topic keywords.

        JVNAUTOSCI-1052: Query Vontology for predicates matching the conversation topic.
        """
        if not keywords:
            return []

        try:
            from src.backend.services.concept_search_service import search_concepts
        except Exception:
            return []

        query = " ".join(keywords[:6])
        try:
            result = search_concepts(
                query=query,
                filter_kind=["predicate"],
                match_type="similarity",
                min_similarity=0.50,
                limit=self._TOPIC_VOCABULARY_MAX_PREDICATES,
            )
        except Exception:
            return []

        entries = result.get("results") if isinstance(result, dict) else None
        if not isinstance(entries, list):
            return []

        predicates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            concept_id = entry.get("concept_id")
            if not isinstance(concept_id, str) or not concept_id:
                continue
            if concept_id in seen:
                continue
            name = entry.get("name")
            score = entry.get("similarity_score")
            predicates.append(
                {
                    "concept_id": concept_id,
                    "name": (
                        self._normalise_preflight_display_name(name) if name else None
                    ),
                    "score": score,
                }
            )
            seen.add(concept_id)

        return predicates

    def _build_topic_vocabulary_section(
        self,
        types: list[dict[str, Any]],
        predicates: list[dict[str, Any]],
        keywords: list[str],
    ) -> str | None:
        """Build the topic vocabulary section for the preflight message."""
        if not types and not predicates:
            return None

        lines: list[str] = [
            f"Topic-relevant vocabulary (keywords: {', '.join(keywords[:5])}):",
        ]

        if types:
            lines.append("  Types:")
            for item in types[: self._TOPIC_VOCABULARY_MAX_TYPES]:
                concept_id = item.get("concept_id")
                name = item.get("name")
                if not concept_id:
                    continue
                if name:
                    lines.append(f'    - {concept_id} (name="{name}")')
                else:
                    lines.append(f"    - {concept_id}")

        if predicates:
            lines.append("  Predicates:")
            for item in predicates[: self._TOPIC_VOCABULARY_MAX_PREDICATES]:
                concept_id = item.get("concept_id")
                name = item.get("name")
                if not concept_id:
                    continue
                if name:
                    lines.append(f'    - {concept_id} (name="{name}")')
                else:
                    lines.append(f"    - {concept_id}")

        return "\n".join(lines)

    def _build_ontology_preflight(
        self,
        prompt: str,
        preferred_language: str | None,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> _OntologyPreflightResult:
        """Deterministically surface preflight predicates and types from the Vontology.

        Stage 1 (JVNAUTOSCI-988): read-only, cheap, and scoped to the current turn.
        Extended (JVNAUTOSCI-1052): includes context-based topic vocabulary discovery.
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

        # --- Topic vocabulary discovery (JVNAUTOSCI-1052) ---
        topic_keywords: list[str] = []
        topic_types: list[dict[str, Any]] = []
        topic_predicates: list[dict[str, Any]] = []
        topic_vocabulary_section: str | None = None
        topic_vocabulary_cached: bool = False

        # Extract keywords from conversation context
        topic_keywords = self._extract_topic_keywords_from_context(raw, context)
        if topic_keywords:
            topic_cache_key = self._get_topic_vocabulary_cache_key(topic_keywords)
            cached_topic = self._topic_vocabulary_cache.get(topic_cache_key)

            if (
                cached_topic
                and (now - cached_topic.get("timestamp", 0))
                < self._TOPIC_VOCABULARY_CACHE_TTL_SECONDS
            ):
                # Use cached topic vocabulary
                topic_types = cached_topic.get("types", [])
                topic_predicates = cached_topic.get("predicates", [])
                topic_vocabulary_cached = True
            else:
                # Discover new topic vocabulary
                topic_types = self._discover_types_for_topic(
                    topic_keywords, preferred_language
                )
                topic_predicates = self._discover_predicates_for_topic(
                    topic_keywords, preferred_language
                )
                # Cache the results
                self._topic_vocabulary_cache[topic_cache_key] = {
                    "timestamp": now,
                    "types": topic_types,
                    "predicates": topic_predicates,
                }

            topic_vocabulary_section = self._build_topic_vocabulary_section(
                topic_types, topic_predicates, topic_keywords
            )

        # Check if we have any vocabulary to surface
        has_vocabulary = (
            predicates
            or explicit_ids
            or targeted_predicates
            or topic_types
            or topic_predicates
        )
        if not has_vocabulary:
            return _OntologyPreflightResult(message=None, telemetry=None)

        lines: list[str] = [
            "ONTOLOGY PRE-FLIGHT (deterministic, read-only; stage=1+topic):",
            "Source: instances of #V#conversation_preflight_predicate + context-based discovery.",
            "Use these existing concept IDs for tool planning. Do not invent new concepts here.",
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

        # Add topic vocabulary section (JVNAUTOSCI-1052)
        if topic_vocabulary_section:
            lines.append("")
            lines.append(topic_vocabulary_section)

        telemetry: dict[str, Any] = {
            "type": "ontology_preflight",
            "stage": "deterministic_preflight",
            "preferred_language": preferred_language,
            "predicate_type": self._PREFLIGHT_PREDICATE_TYPE_ID,
            "preflight_predicates": predicates,
            "explicit_ids": explicit_ids,
            "predicate_query": predicate_query,
            "targeted_predicates": targeted_predicates,
            # JVNAUTOSCI-1052: Topic vocabulary discovery telemetry
            "topic_keywords": topic_keywords,
            "topic_types": topic_types,
            "topic_predicates": topic_predicates,
            "topic_vocabulary_cached": topic_vocabulary_cached,
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

    @staticmethod
    def _extract_concept_ids_from_text(text: str) -> list[str]:
        if not isinstance(text, str) or not text.strip():
            return []
        matches = re.findall(r"#V#[A-Za-z0-9][A-Za-z0-9._-]*", text)
        concept_ids: list[str] = []
        seen: set[str] = set()
        for match in matches:
            cid = str(match).strip().rstrip(".,;:)")
            key = cid.lower()
            if not cid or key in seen:
                continue
            seen.add(key)
            concept_ids.append(cid)
        return concept_ids

    def _infer_required_prompt_tool_retry_tool_calls(
        self,
        *,
        user_text: str,
        missing_required_tools: Sequence[str],
        missing_required_fetch_concept_ids: Sequence[str] | None = None,
        required_create_type_name: str | None = None,
    ) -> list[_ToolCallRequest] | None:
        """Build deterministic tool calls for still-missing explicit requirements."""

        if not missing_required_tools:
            return None

        missing_required_fetch_concept_ids = [
            str(item).strip()
            for item in (missing_required_fetch_concept_ids or [])
            if isinstance(item, str) and str(item).strip()
        ]

        concept_ids = self._extract_concept_ids_from_text(user_text)
        workflow_ids = [
            concept_id
            for concept_id in concept_ids
            if "workflow" in concept_id.lower()
        ]
        primary_workflow_id = workflow_ids[0] if workflow_ids else None

        forced_calls: list[_ToolCallRequest] = []
        for tool_name in missing_required_tools:
            name = str(tool_name).strip()
            if not name:
                continue

            if name == "workflow_list_definitions":
                forced_calls.append(
                    {
                        "action": "call_tool",
                        "tool": name,
                        "payload": {"limit": 50},
                    }
                )
                continue

            if name == "workflow_list_instances":
                if not primary_workflow_id:
                    continue
                forced_calls.append(
                    {
                        "action": "call_tool",
                        "tool": name,
                        "payload": {
                            "workflow_id": primary_workflow_id,
                            "limit": 5,
                        },
                    }
                )
                continue

            if name == "create_concepts":
                requested_name = required_create_type_name
                if not requested_name:
                    requested_name = self._extract_required_create_type_name_from_prompt(
                        user_text
                    )
                if not requested_name:
                    continue
                forced_calls.append(
                    {
                        "action": "call_tool",
                        "tool": name,
                        "payload": {
                            "parent_id": "#V#event",
                            "concepts": [
                                {
                                    "name": requested_name,
                                    "kind": "type",
                                    "description": (
                                        "Fresh test type for workflow runtime verification."
                                    ),
                                }
                            ],
                        },
                    }
                )
                continue

            if name == "fetch_concept":
                fetch_concept_ids = list(missing_required_fetch_concept_ids)
                if not fetch_concept_ids:
                    fetch_concept_ids = (
                        self._extract_required_fetch_concept_ids_from_prompt(user_text)
                    )
                for concept_id in fetch_concept_ids:
                    forced_calls.append(
                        {
                            "action": "call_tool",
                            "tool": name,
                            "payload": {"concept_id": concept_id},
                        }
                    )
                continue

        return forced_calls or None

    def _infer_missing_tool_call_retry_tool_calls(
        self,
        augmented_context: Sequence[Mapping[str, Any]],
        user_prompt: Any | None = None,
        missing_required_tools: Sequence[str] | None = None,
        missing_required_fetch_concept_ids: Sequence[str] | None = None,
        required_create_type_name: str | None = None,
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

        required_forced = self._infer_required_prompt_tool_retry_tool_calls(
            user_text=last_user_text,
            missing_required_tools=(
                list(missing_required_tools) if missing_required_tools else []
            ),
            missing_required_fetch_concept_ids=(
                list(missing_required_fetch_concept_ids)
                if missing_required_fetch_concept_ids
                else []
            ),
            required_create_type_name=(
                str(required_create_type_name).strip()
                if isinstance(required_create_type_name, str)
                and str(required_create_type_name).strip()
                else None
            ),
        )
        if required_forced:
            return required_forced

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
        conversation_session_id: str | None = None,
        turn_id: str | None = None,
        aux_llm_calls: list[Mapping[str, Any]] | None = None,
    ) -> tuple[set[str], str]:
        workflow_result = self.execute_workflow(
            WRITE_TOOL_POLICY_WORKFLOW_ID,
            data={
                "prompt": prompt,
                "requested_write_tools": list(requested_write_tools),
                "recent_user_prompts": list(recent_user_prompts or []),
                "conversation_session_id": conversation_session_id,
                "turn_id": turn_id,
                "aux_llm_calls": aux_llm_calls or [],
                "workflow_episode_source": "chat_turn_workflow",
                "workflow_episode_stage": "write_policy",
            },
            llm_client=llm_client,
            model=model,
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
            trace=trace,
            conversation_session_id=conversation_session_id,
            turn_id=turn_id,
            episode_source="chat_turn_workflow",
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
        environment: WorkflowEnvironment | None = None,
        conversation_session_id: str | None = None,
        turn_id: str | None = None,
        episode_source: str | None = None,
    ):
        if not isinstance(data, dict):
            data = dict(data) if isinstance(data, Mapping) else {}

        resolved_session_id = (
            conversation_session_id
            or data.get("conversation_session_id")
            or data.get("session_id")
        )
        resolved_turn_id = turn_id or data.get("turn_id")
        resolved_source = (
            episode_source
            or data.get("workflow_episode_source")
            or data.get("workflow_source")
            or "workflow_execution"
        )
        resolved_stage = data.get("workflow_episode_stage")
        workflow_registration = None
        workflow_definition_for_identity = None
        workflow_definition_identity: dict[str, Any] | None = None
        try:
            from ...workflows.workflow_definition_identity_service import (
                build_workflow_definition_identity,
            )

            workflow_registration = self._workflow_registry.get_registration(workflow_id)
            workflow_definition_for_identity = (
                workflow_registration.definition
                if workflow_registration is not None
                else self._workflow_registry.get(workflow_id)
            )
            workflow_source = (
                str(getattr(workflow_registration, "source", "") or "").strip()
                if workflow_registration is not None
                else "unknown"
            ) or "unknown"
            workflow_definition_identity = build_workflow_definition_identity(
                workflow_id=workflow_id,
                source=workflow_source,
                definition=workflow_definition_for_identity,
                authoritative_definition=(
                    workflow_definition_for_identity
                    if workflow_source.lower() == "vontology"
                    else None
                ),
            )
        except Exception:
            workflow_registration = None
            workflow_definition_for_identity = None
            workflow_definition_identity = None

        def _safe_scalar_text(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            cleaned = value.strip()
            return cleaned or None

        def _safe_mapping_snapshot(
            payload: Any,
            *,
            max_depth: int = 3,
            max_items: int = 30,
        ) -> dict[str, Any] | None:
            def _walk(value: Any, depth: int) -> Any:
                if depth >= max_depth:
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        return value
                    return str(type(value).__name__)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    return value
                if isinstance(value, Mapping):
                    out: dict[str, Any] = {}
                    for idx, (k, v) in enumerate(value.items()):
                        if idx >= max_items:
                            break
                        if not isinstance(k, str):
                            continue
                        if callable(v):
                            continue
                        out[k] = _walk(v, depth + 1)
                    return out
                if isinstance(value, (list, tuple)):
                    out_list: list[Any] = []
                    for idx, item in enumerate(value):
                        if idx >= max_items:
                            break
                        out_list.append(_walk(item, depth + 1))
                    return out_list
                return str(value)

            if not isinstance(payload, Mapping):
                return None
            snapshot = _walk(payload, 0)
            if isinstance(snapshot, dict):
                return snapshot
            return None

        def _extract_workflow_ids(payload: Any, *, key: str) -> list[str]:
            if not isinstance(payload, Mapping):
                return []
            raw_values = payload.get(key)
            if not isinstance(raw_values, list):
                return []
            workflow_ids: list[str] = []
            seen: set[str] = set()
            for item in raw_values:
                workflow_id: str | None = None
                if isinstance(item, str):
                    workflow_id = _safe_scalar_text(item)
                elif isinstance(item, Mapping):
                    for candidate_key in ("workflow_id", "concept_id", "id"):
                        workflow_id = _safe_scalar_text(item.get(candidate_key))
                        if workflow_id:
                            break
                if not workflow_id:
                    continue
                lowered = workflow_id.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                workflow_ids.append(workflow_id)
            return workflow_ids

        def _derive_selection_rationale(
            *,
            selected_workflow_id: str | None,
            selector_verdict: str | None,
            selector_source: str | None,
            candidate_workflow_ids: Sequence[str],
        ) -> str:
            source = (selector_source or "default").strip().lower() or "default"
            selected = (selected_workflow_id or "").strip()
            verdict = (selector_verdict or "").strip()
            candidate_set = {item.strip().lower() for item in candidate_workflow_ids}
            if source == "selector":
                if selected and selected.lower() in candidate_set:
                    return "selector_selected_discovered_candidate"
                if verdict:
                    return f"selector_verdict:{verdict}"
                return "selector_route_without_explicit_verdict"
            if source == "presenter_mode":
                return "presenter_mode_route"
            if source == "default":
                return "default_routing_fallback"
            return f"routing_source:{source}"

        def _build_turn_execution_selection_snapshot(
            *,
            workflow_data: Any,
            turn_record: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            workflow_routing = (
                workflow_data.get("workflow_routing")
                if isinstance(workflow_data, Mapping)
                else None
            )
            workflow_routing_payload: Mapping[str, Any] = (
                workflow_routing if isinstance(workflow_routing, Mapping) else {}
            )
            workflow_discovery = (
                workflow_data.get("workflow_discovery_result")
                if isinstance(workflow_data, Mapping)
                else None
            )
            workflow_discovery_payload: Mapping[str, Any] = (
                workflow_discovery if isinstance(workflow_discovery, Mapping) else {}
            )

            turn_workflow_selection = (
                turn_record.get("workflow_selection")
                if isinstance(turn_record, Mapping)
                else None
            )
            turn_workflow_selection_payload: Mapping[str, Any] = (
                turn_workflow_selection
                if isinstance(turn_workflow_selection, Mapping)
                else {}
            )
            turn_workflow_discovery = turn_workflow_selection_payload.get(
                "workflow_discovery"
            )
            turn_workflow_discovery_payload: Mapping[str, Any] = (
                turn_workflow_discovery
                if isinstance(turn_workflow_discovery, Mapping)
                else {}
            )

            selected_workflow_id = (
                _safe_scalar_text(turn_workflow_selection_payload.get("selected_workflow_id"))
                or _safe_scalar_text(workflow_routing_payload.get("workflow_id"))
                or _safe_scalar_text(workflow_id)
            )
            selector_verdict = (
                _safe_scalar_text(turn_workflow_selection_payload.get("selector_verdict"))
                or _safe_scalar_text(workflow_routing_payload.get("verdict"))
            )
            selector_source = (
                _safe_scalar_text(turn_workflow_selection_payload.get("selector_source"))
                or _safe_scalar_text(workflow_routing_payload.get("source"))
                or "default"
            )

            candidate_workflow_ids = (
                _extract_workflow_ids(workflow_discovery_payload, key="candidate_ids")
                or _extract_workflow_ids(workflow_discovery_payload, key="candidates")
                or _extract_workflow_ids(workflow_discovery_payload, key="matches")
                or _extract_workflow_ids(
                    turn_workflow_discovery_payload, key="candidate_ids"
                )
            )
            excluded_candidate_ids = (
                _extract_workflow_ids(
                    workflow_discovery_payload, key="excluded_candidate_ids"
                )
                or _extract_workflow_ids(workflow_discovery_payload, key="excluded")
                or _extract_workflow_ids(
                    workflow_discovery_payload, key="excluded_candidates"
                )
                or _extract_workflow_ids(
                    turn_workflow_discovery_payload, key="excluded_candidate_ids"
                )
            )

            selection_rationale = (
                _safe_scalar_text(workflow_routing_payload.get("selection_rationale"))
                or _safe_scalar_text(turn_workflow_selection_payload.get("selection_rationale"))
                or _derive_selection_rationale(
                    selected_workflow_id=selected_workflow_id,
                    selector_verdict=selector_verdict,
                    selector_source=selector_source,
                    candidate_workflow_ids=candidate_workflow_ids,
                )
            )

            return {
                "selected_workflow_id": selected_workflow_id,
                "selector_verdict": selector_verdict,
                "selector_source": selector_source,
                "selection_rationale": selection_rationale,
                "candidate_workflow_ids": list(candidate_workflow_ids),
                "excluded_candidate_ids": list(excluded_candidate_ids),
            }

        def _extract_stage_tokens_from_diagnostics(
            diagnostics_payload: Mapping[str, Any] | None,
        ) -> list[str]:
            if not isinstance(diagnostics_payload, Mapping):
                return []

            stages: list[str] = []

            phase_history = diagnostics_payload.get("phase_history")
            if isinstance(phase_history, list):
                for phase_entry in phase_history:
                    if not isinstance(phase_entry, Mapping):
                        continue
                    phase = _safe_scalar_text(phase_entry.get("phase"))
                    if phase:
                        stages.append(phase)

            progress_events = diagnostics_payload.get("progress_events")
            if isinstance(progress_events, list):
                for event in progress_events:
                    if not isinstance(event, Mapping):
                        continue
                    stage = _safe_scalar_text(event.get("stage"))
                    if stage:
                        stages.append(stage)

            return stages

        def _collect_turn_runtime_stage_sequence(
            *,
            workflow_data: Any,
            final_state: str | None = None,
            terminal_stage: str | None = None,
        ) -> list[str]:
            runtime_stages: list[str] = []

            if isinstance(resolved_stage, str) and resolved_stage.strip():
                runtime_stages.append(resolved_stage.strip())

            diagnostics_sources: list[Mapping[str, Any]] = []
            if isinstance(data.get("turn_execution_diagnostics"), Mapping):
                diagnostics_sources.append(
                    cast(Mapping[str, Any], data.get("turn_execution_diagnostics"))
                )
            if isinstance(workflow_data, Mapping):
                workflow_diagnostics = workflow_data.get("turn_execution_diagnostics")
                if isinstance(workflow_diagnostics, Mapping):
                    diagnostics_sources.append(workflow_diagnostics)
                turn_record = workflow_data.get("turn_execution_record")
                if isinstance(turn_record, Mapping):
                    execution_payload = turn_record.get("execution")
                    if isinstance(execution_payload, Mapping):
                        diagnostic_events = execution_payload.get("diagnostic_events")
                        if isinstance(diagnostic_events, list):
                            for event in diagnostic_events:
                                if not isinstance(event, Mapping):
                                    continue
                                phase = _safe_scalar_text(event.get("phase"))
                                stage = _safe_scalar_text(event.get("stage"))
                                if phase:
                                    runtime_stages.append(phase)
                                if stage:
                                    runtime_stages.append(stage)

            for diagnostics_payload in diagnostics_sources:
                runtime_stages.extend(
                    _extract_stage_tokens_from_diagnostics(diagnostics_payload)
                )

            if isinstance(final_state, str) and final_state.strip():
                runtime_stages.append(final_state.strip())
            if isinstance(terminal_stage, str) and terminal_stage.strip():
                runtime_stages.append(terminal_stage.strip())

            return runtime_stages

        def _build_turn_execution_stage_path_snapshot(
            *,
            workflow_data: Any,
            final_state: str | None = None,
            terminal_stage: str | None = None,
        ) -> dict[str, Any]:
            return build_conversation_turn_stage_path(
                runtime_stages=_collect_turn_runtime_stage_sequence(
                    workflow_data=workflow_data,
                    final_state=final_state,
                    terminal_stage=terminal_stage,
                ),
                workflow_id=_safe_scalar_text(workflow_id),
            )

        def _build_turn_execution_summary(payload: Any) -> dict[str, Any] | None:
            if not isinstance(payload, Mapping):
                return None
            raw_completion_gate = payload.get("completion_gate")
            completion_gate: Mapping[str, Any] = (
                raw_completion_gate if isinstance(raw_completion_gate, Mapping) else {}
            )
            raw_workflow_selection = payload.get("workflow_selection")
            workflow_selection: Mapping[str, Any] = (
                raw_workflow_selection
                if isinstance(raw_workflow_selection, Mapping)
                else {}
            )
            raw_required_effects = payload.get("required_effects")
            required_effects: list[Any] = (
                raw_required_effects if isinstance(raw_required_effects, list) else []
            )
            unresolved_effect_count = 0
            for effect in required_effects:
                if not isinstance(effect, Mapping):
                    continue
                status = effect.get("status")
                if isinstance(status, str) and status in {"not_executed", "not_satisfied"}:
                    unresolved_effect_count += 1
            selection = _build_turn_execution_selection_snapshot(
                workflow_data={"turn_execution_record": payload}
                if isinstance(payload, Mapping)
                else {},
                turn_record=payload,
            )
            return {
                "request_id": _safe_scalar_text(payload.get("request_id")),
                "decision": completion_gate.get("decision"),
                "decision_reason": completion_gate.get("decision_reason"),
                "requires_follow_up": completion_gate.get("requires_follow_up"),
                "safe_to_claim_completion": completion_gate.get(
                    "safe_to_claim_completion"
                ),
                "blocking_effect_ids": completion_gate.get("blocking_effect_ids"),
                "selected_workflow_id": selection.get("selected_workflow_id")
                or workflow_selection.get("selected_workflow_id"),
                "selector_verdict": selection.get("selector_verdict")
                or workflow_selection.get("selector_verdict"),
                "required_effect_count": len(required_effects),
                "unresolved_effect_count": unresolved_effect_count,
            }

        def _build_turn_execution_contract_snapshot(
            workflow_data: Any,
        ) -> dict[str, Any]:
            prompt = (
                workflow_data.get("prompt")
                if isinstance(workflow_data, Mapping)
                else data.get("prompt")
            )
            prompt_preview = prompt[:1000] if isinstance(prompt, str) else None
            selection = _build_turn_execution_selection_snapshot(
                workflow_data=workflow_data,
                turn_record=(
                    workflow_data.get("turn_execution_record")
                    if isinstance(workflow_data, Mapping)
                    and isinstance(workflow_data.get("turn_execution_record"), Mapping)
                    else None
                ),
            )
            stage_path = _build_turn_execution_stage_path_snapshot(
                workflow_data=workflow_data
            )
            return {
                "schema_version": "turn_execution_contract.v1",
                "workflow_id": _safe_scalar_text(workflow_id),
                "request_id": _safe_scalar_text(resolved_turn_id),
                "conversation_session_id": _safe_scalar_text(resolved_session_id),
                "turn_id": _safe_scalar_text(resolved_turn_id),
                "episode_source": str(resolved_source),
                "episode_stage": (
                    str(resolved_stage).strip()
                    if isinstance(resolved_stage, str) and resolved_stage.strip()
                    else None
                ),
                "prompt_preview": prompt_preview,
                "selection": selection,
                # Canonical mapping of runtime stage tokens into one
                # conversation-turn orchestration representation.
                "workflow_stage_model": build_conversation_turn_stage_model_snapshot(),
                "workflow_stage_path": stage_path,
                "workflow_definition_identity": workflow_definition_identity,
            }

        def _build_turn_execution_outcome(
            *,
            workflow_data: Any,
            completed: bool,
            final_state: str | None,
            terminal_stage: str | None,
            termination_code: str | None,
            termination_detail: str | None,
        ) -> dict[str, Any] | None:
            if not isinstance(workflow_data, Mapping):
                return None

            raw_turn_record = workflow_data.get("turn_execution_record")
            turn_record: Mapping[str, Any] = (
                raw_turn_record if isinstance(raw_turn_record, Mapping) else {}
            )
            if not turn_record and not any(
                key in workflow_data
                for key in (
                    "completion_gate_decision",
                    "completion_gate_requires_follow_up",
                    "completion_gate_safe_to_claim_completion",
                    "critic_summary",
                )
            ):
                return None

            required_effects_raw = turn_record.get("required_effects")
            required_effects: list[Mapping[str, Any]] = (
                [item for item in required_effects_raw if isinstance(item, Mapping)]
                if isinstance(required_effects_raw, list)
                else []
            )
            postcondition_checks_raw = turn_record.get("postcondition_checks")
            postcondition_checks: list[Mapping[str, Any]] = (
                [item for item in postcondition_checks_raw if isinstance(item, Mapping)]
                if isinstance(postcondition_checks_raw, list)
                else []
            )
            critic_payload_raw = turn_record.get("critic")
            critic_payload: Mapping[str, Any] = (
                critic_payload_raw if isinstance(critic_payload_raw, Mapping) else {}
            )
            critic_summary_raw = workflow_data.get("critic_summary")
            if not isinstance(critic_summary_raw, Mapping):
                critic_summary_raw = critic_payload.get("summary")
            critic_summary: Mapping[str, Any] = (
                critic_summary_raw if isinstance(critic_summary_raw, Mapping) else {}
            )

            completion_gate_raw = turn_record.get("completion_gate")
            completion_gate: Mapping[str, Any] = (
                completion_gate_raw if isinstance(completion_gate_raw, Mapping) else {}
            )
            decision = _safe_scalar_text(workflow_data.get("completion_gate_decision")) or (
                _safe_scalar_text(completion_gate.get("decision"))
                or ("completed" if completed else "failed")
            )
            decision_reason = _safe_scalar_text(
                workflow_data.get("completion_gate_decision_reason")
            ) or _safe_scalar_text(completion_gate.get("decision_reason"))
            requires_follow_up = bool(
                workflow_data.get(
                    "completion_gate_requires_follow_up",
                    completion_gate.get("requires_follow_up", False),
                )
            )
            safe_to_claim_completion = bool(
                workflow_data.get(
                    "completion_gate_safe_to_claim_completion",
                    completion_gate.get("safe_to_claim_completion", not requires_follow_up),
                )
            )

            blocking_effect_ids_raw = workflow_data.get("completion_gate_blocking_effect_ids")
            if not isinstance(blocking_effect_ids_raw, list):
                blocking_effect_ids_raw = completion_gate.get("blocking_effect_ids")
            blocking_effect_ids: list[str] = []
            if isinstance(blocking_effect_ids_raw, list):
                for item in blocking_effect_ids_raw:
                    item_text = _safe_scalar_text(item)
                    if item_text:
                        blocking_effect_ids.append(item_text)

            check_instances: list[dict[str, Any]] = []
            for check in postcondition_checks:
                observed_payload = check.get("observed")
                check_instances.append(
                    {
                        "check_id": _safe_scalar_text(check.get("check_id")),
                        "effect_id": _safe_scalar_text(check.get("effect_id")),
                        "check_type": _safe_scalar_text(check.get("check_type")),
                        "check_tool": _safe_scalar_text(check.get("check_tool")),
                        "status": _safe_scalar_text(check.get("status")),
                        "evidence": _safe_scalar_text(check.get("evidence")),
                        "error": _safe_scalar_text(check.get("error")),
                        "observed": (
                            _safe_mapping_snapshot(observed_payload, max_depth=2, max_items=20)
                            if isinstance(observed_payload, Mapping)
                            else None
                        ),
                    }
                )

            required_effect_instances: list[dict[str, Any]] = []
            for effect in required_effects:
                required_effect_instances.append(
                    {
                        "effect_id": _safe_scalar_text(effect.get("effect_id")),
                        "effect_type": _safe_scalar_text(effect.get("effect_type")),
                        "status": _safe_scalar_text(effect.get("status")),
                        "status_reason": _safe_scalar_text(effect.get("status_reason")),
                        "postcondition_required": bool(
                            effect.get("postcondition_required", False)
                        ),
                    }
                )

            critic_workflow_id = _safe_scalar_text(critic_payload.get("workflow_id"))
            completion_gate_workflow_id = _safe_scalar_text(
                completion_gate.get("workflow_id")
            ) or TURN_COMPLETION_GATE_WORKFLOW_ID
            step_instances = [
                {
                    "step_id": "step_turn_execution_critic",
                    "step_key": "turn_execution_critic",
                    "workflow_id": (
                        critic_workflow_id
                        or KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID
                    ),
                    "status": "completed",
                    "evidence": {
                        "check_count": len(check_instances),
                        "required_effect_count": len(required_effect_instances),
                    },
                },
                {
                    "step_id": "step_turn_completion_gate",
                    "step_key": "turn_completion_gate",
                    "workflow_id": completion_gate_workflow_id,
                    "status": "completed",
                    "evidence": {
                        "decision": decision,
                        "requires_follow_up": requires_follow_up,
                    },
                },
            ]

            selection = _build_turn_execution_selection_snapshot(
                workflow_data=workflow_data,
                turn_record=turn_record,
            )
            stage_path = _build_turn_execution_stage_path_snapshot(
                workflow_data=workflow_data,
                final_state=final_state,
                terminal_stage=terminal_stage,
            )

            not_verified_count = int(critic_summary.get("not_verified_count") or 0)
            inconclusive_count = int(critic_summary.get("inconclusive_count") or 0)
            error_count = int(critic_summary.get("error_count") or 0)
            unresolved_check_count = not_verified_count + inconclusive_count + error_count

            return {
                "schema_version": "turn_execution_outcome.v1",
                "workflow_instance_id": durable_instance_id,
                "workflow_id": _safe_scalar_text(workflow_id),
                "request_id": _safe_scalar_text(turn_record.get("request_id"))
                or _safe_scalar_text(resolved_turn_id),
                "session_id": _safe_scalar_text(turn_record.get("session_id"))
                or _safe_scalar_text(resolved_session_id),
                "turn_id": _safe_scalar_text(resolved_turn_id),
                "selection": selection,
                "workflow_stage_path": stage_path,
                "step_instances": step_instances,
                "required_effect_instances": required_effect_instances,
                "check_instances": check_instances,
                "critic_verdict": {
                    "workflow_id": (
                        critic_workflow_id
                        or KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID
                    ),
                    "summary": _safe_mapping_snapshot(critic_summary, max_depth=2, max_items=20)
                    or {},
                    "has_unresolved_checks": unresolved_check_count > 0,
                    "unresolved_check_count": unresolved_check_count,
                },
                "completion_state": {
                    "decision": decision,
                    "decision_reason": decision_reason,
                    "safe_to_claim_completion": safe_to_claim_completion,
                    "requires_follow_up": requires_follow_up,
                    "blocking_effect_ids": blocking_effect_ids,
                },
                "terminal": {
                    "completed": bool(completed),
                    "final_state": final_state,
                    "terminal_stage": terminal_stage,
                    "termination_code": termination_code,
                    "termination_detail": termination_detail,
                },
            }

        def _build_turn_execution_runtime_snapshot(
            *,
            workflow_data: Any,
            completed: bool,
            final_state: str | None,
            terminal_stage: str | None,
            termination_code: str | None,
            termination_detail: str | None,
        ) -> dict[str, Any]:
            outcome = _build_turn_execution_outcome(
                workflow_data=workflow_data,
                completed=completed,
                final_state=final_state,
                terminal_stage=terminal_stage,
                termination_code=termination_code,
                termination_detail=termination_detail,
            )
            contract = _build_turn_execution_contract_snapshot(workflow_data)
            runtime: dict[str, Any] = {
                "schema_version": "turn_execution_runtime.v1",
                "workflow_instance_id": durable_instance_id,
                "updated_at_utc": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "contract": contract,
                "workflow_definition_identity": workflow_definition_identity,
                "terminal_stage": terminal_stage,
                "termination_code": termination_code,
            }
            stage_path = (
                outcome.get("workflow_stage_path")
                if isinstance(outcome, dict)
                else None
            )
            if not isinstance(stage_path, Mapping):
                stage_path = _build_turn_execution_stage_path_snapshot(
                    workflow_data=workflow_data,
                    final_state=final_state,
                    terminal_stage=terminal_stage,
                )
            runtime["workflow_stage_path"] = stage_path
            if isinstance(outcome, dict):
                runtime["request_id"] = outcome.get("request_id")
                runtime["selection"] = outcome.get("selection")
                runtime["step_instances"] = outcome.get("step_instances", [])
                runtime["check_instances"] = outcome.get("check_instances", [])
                runtime["completion_state"] = outcome.get("completion_state")
            else:
                runtime["request_id"] = _safe_scalar_text(resolved_turn_id)
                runtime["selection"] = contract.get("selection")
                runtime["step_instances"] = []
                runtime["check_instances"] = []
                runtime["completion_state"] = {
                    "decision": "failed" if not completed else "completed",
                    "decision_reason": _safe_scalar_text(termination_detail),
                    "safe_to_claim_completion": bool(completed),
                    "requires_follow_up": not bool(completed),
                    "blocking_effect_ids": [],
                }
            return runtime

        resolved_user_id = _safe_scalar_text(data.get("user_concept_id"))
        resolved_org_id = _safe_scalar_text(data.get("org_concept_id"))
        resolved_namespace = (
            _safe_scalar_text(user_namespace)
            or _safe_scalar_text(data.get("namespace"))
            or _safe_scalar_text(data.get("user_namespace"))
        )

        durable_instance_manager = None
        durable_instance_id: str | None = None
        durable_instance_created_new: bool | None = None

        def _build_durable_inputs_snapshot() -> dict[str, Any]:
            prompt = data.get("prompt")
            prompt_preview = prompt[:1000] if isinstance(prompt, str) else None
            return {
                "conversation_session_id": _safe_scalar_text(resolved_session_id),
                "turn_id": _safe_scalar_text(resolved_turn_id),
                "episode_source": str(resolved_source),
                "episode_stage": (
                    str(resolved_stage).strip()
                    if isinstance(resolved_stage, str) and resolved_stage.strip()
                    else None
                ),
                "prompt_preview": prompt_preview,
                "workflow_routing": _safe_mapping_snapshot(data.get("workflow_routing")),
                "workflow_discovery_result": _safe_mapping_snapshot(
                    data.get("workflow_discovery_result")
                ),
                "workflow_definition_identity": workflow_definition_identity,
                # Canonical per-turn contract persisted at workflow start.
                "turn_execution_contract": _build_turn_execution_contract_snapshot(data),
            }

        def _build_durable_outputs_snapshot(
            *,
            completed: bool,
            final_state: str | None,
            terminal_stage: str | None,
            termination_code: str | None,
            termination_detail: str | None,
            workflow_data: Any,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "completed": bool(completed),
                "final_state": final_state,
                "termination_code": termination_code,
                "termination_detail": termination_detail,
                "workflow_definition_identity": workflow_definition_identity,
            }
            if not isinstance(workflow_data, Mapping):
                return payload

            turn_record = workflow_data.get("turn_execution_record")
            turn_summary = _build_turn_execution_summary(turn_record)
            if isinstance(turn_summary, dict):
                payload["turn_execution"] = turn_summary

            critic_summary = workflow_data.get("critic_summary")
            if isinstance(critic_summary, Mapping):
                payload["critic_summary"] = _safe_mapping_snapshot(critic_summary)

            for key in (
                "completion_gate_decision",
                "completion_gate_decision_reason",
                "completion_gate_requires_follow_up",
                "completion_gate_safe_to_claim_completion",
            ):
                if key in workflow_data:
                    payload[key] = workflow_data.get(key)

            workflow_routing = workflow_data.get("workflow_routing")
            if isinstance(workflow_routing, Mapping):
                payload["workflow_routing"] = _safe_mapping_snapshot(workflow_routing)
            workflow_discovery = workflow_data.get("workflow_discovery_result")
            if isinstance(workflow_discovery, Mapping):
                payload["workflow_discovery_result"] = _safe_mapping_snapshot(
                    workflow_discovery
                )

            turn_execution_outcome = _build_turn_execution_outcome(
                workflow_data=workflow_data,
                completed=completed,
                final_state=final_state,
                terminal_stage=terminal_stage,
                termination_code=termination_code,
                termination_detail=termination_detail,
            )
            if isinstance(turn_execution_outcome, dict):
                payload["turn_execution_outcome"] = turn_execution_outcome

            return payload

        def _finalise_durable_instance(
            *,
            completed: bool,
            final_state: str | None,
            terminal_stage: str | None,
            termination_code: str | None,
            termination_detail: str | None,
            workflow_data: Any,
        ) -> None:
            if durable_instance_manager is None or not isinstance(durable_instance_id, str):
                return
            try:
                runtime_snapshot = _build_turn_execution_runtime_snapshot(
                    workflow_data=workflow_data,
                    completed=completed,
                    final_state=final_state,
                    terminal_stage=terminal_stage,
                    termination_code=termination_code,
                    termination_detail=termination_detail,
                )
                try:
                    durable_instance_manager.checkpoint(
                        durable_instance_id,
                        current_state=terminal_stage
                        or final_state
                        or ("completed" if completed else "terminated"),
                        workflow_data={"turn_execution_runtime": runtime_snapshot},
                        progress_message="turn_execution_runtime_persisted",
                    )
                except Exception as checkpoint_exc:
                    self._logger.warning(
                        "[workflow_instance_telemetry] Failed to checkpoint runtime "
                        "snapshot for %s: %s",
                        durable_instance_id,
                        checkpoint_exc,
                    )
                if completed:
                    durable_instance_manager.mark_completed(
                        durable_instance_id,
                        outputs=_build_durable_outputs_snapshot(
                            completed=True,
                            final_state=final_state,
                            terminal_stage=terminal_stage,
                            termination_code=termination_code,
                            termination_detail=termination_detail,
                            workflow_data=workflow_data,
                        ),
                        final_state=final_state or terminal_stage or "completed",
                    )
                    return
                failure_code = (
                    str(termination_code).strip().lower()
                    if isinstance(termination_code, str) and termination_code.strip()
                    else "terminated"
                )
                failure_detail = (
                    str(termination_detail).strip()
                    if isinstance(termination_detail, str) and termination_detail.strip()
                    else "workflow_execution_terminated"
                )
                durable_instance_manager.mark_failed(
                    durable_instance_id,
                    error=f"{failure_code}:{failure_detail}",
                    error_step=terminal_stage,
                    increment_retry=False,
                )
            except Exception as exc:
                self._logger.warning(
                    "[workflow_instance_telemetry] Failed to finalise durable instance "
                    "%s for workflow %s: %s",
                    durable_instance_id,
                    workflow_id,
                    exc,
                )

        if (
            isinstance(workflow_id, str)
            and workflow_id.strip()
            and resolved_user_id
            and resolved_org_id
            and resolved_namespace
        ):
            try:
                from ...workflows.durable import WorkflowInstanceManager

                durable_instance_manager = WorkflowInstanceManager()
                source_event_type = (
                    str(resolved_source).strip() if str(resolved_source).strip() else None
                )
                source_event_id = (
                    _safe_scalar_text(resolved_turn_id)
                    or _safe_scalar_text(resolved_session_id)
                )
                if source_event_type and source_event_id:
                    idempotency_key = (
                        "orchestrator.execute_workflow:"
                        f"{workflow_id}:{source_event_type}:{source_event_id}:"
                        f"{str(resolved_stage or '').strip()}"
                    )
                    (
                        durable_instance_id,
                        durable_instance_created_new,
                    ) = durable_instance_manager.create_instance_for_event(
                        workflow_id=workflow_id,
                        user_id=resolved_user_id,
                        org_id=resolved_org_id,
                        namespace=resolved_namespace,
                        event_idempotency_key=idempotency_key,
                        source_event_type=source_event_type,
                        source_event_id=source_event_id,
                        inputs=_build_durable_inputs_snapshot(),
                        max_retries=0,
                    )
                else:
                    durable_instance_id = durable_instance_manager.create_instance(
                        workflow_id,
                        user_id=resolved_user_id,
                        org_id=resolved_org_id,
                        namespace=resolved_namespace,
                        inputs=_build_durable_inputs_snapshot(),
                        max_retries=0,
                    )
                    durable_instance_created_new = True
            except Exception as exc:
                self._logger.warning(
                    "[workflow_instance_telemetry] Failed to create durable instance "
                    "for workflow %s: %s",
                    workflow_id,
                    exc,
                )
                durable_instance_manager = None
                durable_instance_id = None
                durable_instance_created_new = None

        episode_id: str | None = None
        stable_key: str | None = None
        start_episode_fn = None
        finalise_episode_fn = None
        build_episode_key_fn = None
        try:
            from ...services.workflow_episode_service import (
                build_workflow_episode_stable_key,
                finalise_workflow_use_episode,
                start_workflow_use_episode,
            )

            start_episode_fn = start_workflow_use_episode
            finalise_episode_fn = finalise_workflow_use_episode
            build_episode_key_fn = build_workflow_episode_stable_key
        except Exception:
            start_episode_fn = None
            finalise_episode_fn = None
            build_episode_key_fn = None

        def _normalise_error_code(error_value: Any) -> str:
            if not isinstance(error_value, str) or not error_value.strip():
                return "terminated"
            cleaned_error = error_value.strip()
            if ":" in cleaned_error:
                return cleaned_error.split(":", 1)[0].strip().lower() or "terminated"
            return "terminated"

        def _record_episode_telemetry(
            *,
            completed: bool,
            terminal_stage: str | None,
            final_state: str | None,
            termination_code: str | None,
            termination_detail: str | None,
        ) -> None:
            aux_log = data.get("aux_llm_calls")
            if not isinstance(aux_log, list):
                return
            aux_log.append(
                {
                    "type": "workflow_use_episode",
                    "episode_id": episode_id,
                    "workflow_instance_id": durable_instance_id,
                    "workflow_instance_created_new": durable_instance_created_new,
                    "workflow_id": workflow_id,
                    "source": str(resolved_source),
                    "session_id": (
                        str(resolved_session_id).strip()
                        if isinstance(resolved_session_id, str)
                        and resolved_session_id.strip()
                        else None
                    ),
                    "turn_id": (
                        str(resolved_turn_id).strip()
                        if isinstance(resolved_turn_id, str) and resolved_turn_id.strip()
                        else None
                    ),
                    "completed": bool(completed),
                    "terminal_stage": terminal_stage,
                    "final_state": final_state,
                    "termination_reason": {
                        "code": termination_code,
                        "detail": termination_detail,
                    },
                    "workflow_definition_identity": workflow_definition_identity,
                }
            )

        if (
            callable(start_episode_fn)
            and callable(build_episode_key_fn)
            and isinstance(workflow_id, str)
            and workflow_id.strip()
        ):
            try:
                stable_key = build_episode_key_fn(
                    workflow_id=workflow_id,
                    source=str(resolved_source),
                    turn_id=(
                        str(resolved_turn_id).strip()
                        if isinstance(resolved_turn_id, str)
                        and resolved_turn_id.strip()
                        else None
                    ),
                    session_id=(
                        str(resolved_session_id).strip()
                        if isinstance(resolved_session_id, str)
                        and resolved_session_id.strip()
                        else None
                    ),
                    stage=(
                        str(resolved_stage).strip()
                        if isinstance(resolved_stage, str) and resolved_stage.strip()
                        else None
                    ),
                )
                start_payload = start_episode_fn(
                    workflow_id=workflow_id,
                    source=str(resolved_source),
                    namespace=user_namespace,
                    session_id=(
                        str(resolved_session_id).strip()
                        if isinstance(resolved_session_id, str)
                        and resolved_session_id.strip()
                        else None
                    ),
                    turn_id=(
                        str(resolved_turn_id).strip()
                        if isinstance(resolved_turn_id, str) and resolved_turn_id.strip()
                        else None
                    ),
                    stable_key=stable_key,
                    metadata={
                        "stage": resolved_stage,
                        "path": "orchestrator.execute_workflow",
                        "workflow_definition_identity": workflow_definition_identity,
                    },
                )
                if isinstance(start_payload, Mapping):
                    payload_episode_id = start_payload.get("episode_id")
                    if isinstance(payload_episode_id, str) and payload_episode_id:
                        episode_id = payload_episode_id
            except Exception:
                episode_id = None

        workflow_def = (
            workflow_definition_for_identity
            if workflow_definition_for_identity is not None
            else self._workflow_registry.get(workflow_id)
        )
        if workflow_def is None:
            if callable(finalise_episode_fn):
                try:
                    finalise_episode_fn(
                        workflow_id=workflow_id,
                        episode_id=episode_id,
                        stable_key=stable_key,
                        completed=False,
                        terminal_stage="workflow_lookup",
                        final_state=None,
                        termination_code="workflow_not_registered",
                        termination_detail="workflow_definition_not_found",
                        metadata={
                            "path": "orchestrator.execute_workflow",
                            "workflow_definition_identity": workflow_definition_identity,
                        },
                    )
                except Exception:
                    pass
            _record_episode_telemetry(
                completed=False,
                terminal_stage="workflow_lookup",
                final_state=None,
                termination_code="workflow_not_registered",
                termination_detail="workflow_definition_not_found",
            )
            _finalise_durable_instance(
                completed=False,
                final_state=None,
                terminal_stage="workflow_lookup",
                termination_code="workflow_not_registered",
                termination_detail="workflow_definition_not_found",
                workflow_data=data,
            )
            return None

        env = environment or WorkflowEnvironment(
            llm_client=llm_client,
            gateway=self._gateway,
            model=model,
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
            max_tool_invocations=self._max_tool_invocations,
            default_gmail_profile=self._default_gmail_profile,
        )
        try:
            result = self._workflow_executor.run(
                workflow_def,
                environment=env,
                data=data,
                trace=trace,
            )
            completed = bool(getattr(result, "completed", False))
            final_state = (
                str(result.final_state)
                if isinstance(getattr(result, "final_state", None), str)
                else None
            )
            error_value = result.error if isinstance(result.error, str) else None
            termination_code = "completed" if completed else _normalise_error_code(
                error_value
            )
            termination_detail = None if completed else error_value
            terminal_stage = final_state or ("completed" if completed else "terminated")
            if callable(finalise_episode_fn):
                try:
                    finalise_episode_fn(
                        workflow_id=workflow_id,
                        episode_id=episode_id,
                        stable_key=stable_key,
                        completed=completed,
                        terminal_stage=terminal_stage,
                        final_state=final_state,
                        termination_code=termination_code,
                        termination_detail=termination_detail,
                        metadata={
                            "path": "orchestrator.execute_workflow",
                            "workflow_definition_identity": workflow_definition_identity,
                        },
                    )
                except Exception:
                    pass
            _record_episode_telemetry(
                completed=completed,
                terminal_stage=terminal_stage,
                final_state=final_state,
                termination_code=termination_code,
                termination_detail=termination_detail,
            )
            _finalise_durable_instance(
                completed=completed,
                final_state=final_state,
                terminal_stage=terminal_stage,
                termination_code=termination_code,
                termination_detail=termination_detail,
                workflow_data=getattr(result, "data", None),
            )
            return result
        except Exception as exc:
            if callable(finalise_episode_fn):
                try:
                    finalise_episode_fn(
                        workflow_id=workflow_id,
                        episode_id=episode_id,
                        stable_key=stable_key,
                        completed=False,
                        terminal_stage="workflow_exception",
                        final_state=None,
                        termination_code="exception",
                        termination_detail=str(exc),
                        metadata={
                            "path": "orchestrator.execute_workflow",
                            "workflow_definition_identity": workflow_definition_identity,
                        },
                    )
                except Exception:
                    pass
            _record_episode_telemetry(
                completed=False,
                terminal_stage="workflow_exception",
                final_state=None,
                termination_code="exception",
                termination_detail=str(exc),
            )
            _finalise_durable_instance(
                completed=False,
                final_state=None,
                terminal_stage="workflow_exception",
                termination_code="exception",
                termination_detail=str(exc),
                workflow_data=data,
            )
            raise

    def _get_auto_proceed_minimal_imposition_enabled(self) -> bool:
        """Return whether minimal-imposition auto-proceed is enabled.

        Defaults to enabled so the assistant avoids avoidable human hand-offs.
        """

        try:
            from src.backend.services.settings_service import (
                get_auto_proceed_minimal_imposition_enabled,
            )

            return bool(get_auto_proceed_minimal_imposition_enabled())
        except Exception:
            return self._env_flag_enabled(
                "VON_AUTO_PROCEED_MINIMAL_IMPOSITION_ENABLE",
                default="1",
            )

    @classmethod
    def _assess_minimal_imposition_auto_proceed(
        cls,
        response_text: str,
    ) -> Mapping[str, Any]:
        """Assess whether autonomous continuation minimises user imposition."""

        if not isinstance(response_text, str) or not response_text.strip():
            return {
                "should_auto_proceed": False,
                "reason": "empty_response",
                "has_progress_promise": False,
                "has_intent_language": False,
                "has_remaining_work_signal": False,
                "asks_for_user_decision": False,
                "contains_question_mark": False,
            }

        text = response_text.strip()
        normalised = (
            text.replace("\u2019", "'")
            .replace("\u2018", "'")
            .replace("\u2032", "'")
            .replace("\u201c", '"')
            .replace("\u201d", '"')
        )
        lowered = normalised.lower()

        has_progress_promise = bool(
            cls._AUTO_PROCEED_PROGRESS_PROMISE_PATTERN.search(lowered)
        )
        has_intent_language = bool(cls._COMPLETION_CLAIM_INTENT_PATTERN.search(lowered))
        has_remaining_work_signal = any(
            token in lowered
            for token in (
                "remaining",
                "still need",
                "next",
                "to complete",
                "to finish",
                "continue",
            )
        )
        asks_for_user_decision = bool(
            cls._AUTO_PROCEED_USER_DECISION_PATTERN.search(lowered)
        )
        contains_question_mark = "?" in normalised

        should_auto_proceed = False
        reason = "no_progress_signal"
        if asks_for_user_decision:
            reason = "user_decision_requested"
        elif contains_question_mark and not has_progress_promise:
            reason = "question_without_progress_promise"
        elif has_progress_promise or (has_intent_language and has_remaining_work_signal):
            should_auto_proceed = True
            reason = "minimal_imposition_pass"

        return {
            "should_auto_proceed": should_auto_proceed,
            "reason": reason,
            "has_progress_promise": has_progress_promise,
            "has_intent_language": has_intent_language,
            "has_remaining_work_signal": has_remaining_work_signal,
            "asks_for_user_decision": asks_for_user_decision,
            "contains_question_mark": contains_question_mark,
        }

    @staticmethod
    def _env_flag_enabled(name: str, *, default: str = "0") -> bool:
        value = os.getenv(name, default)
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _env_csv_values(name: str, *, default: str = "") -> tuple[str, ...]:
        raw = os.getenv(name, default)
        if not isinstance(raw, str) or not raw.strip():
            return ()
        values: list[str] = []
        seen: set[str] = set()
        for part in raw.split(","):
            candidate = part.strip()
            if not candidate:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            values.append(candidate)
        return tuple(values)

    @staticmethod
    def _extract_discovery_candidates(
        workflow_discovery_result: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Extract discovery candidates with backward-compatible key handling."""
        raw_candidates = workflow_discovery_result.get("candidates")
        if not isinstance(raw_candidates, list):
            raw_candidates = workflow_discovery_result.get("matches")
        if not isinstance(raw_candidates, list):
            return []
        return [dict(item) for item in raw_candidates if isinstance(item, Mapping)]

    def _prepare_selector_discovered_matches(
        self,
        workflow_discovery_result: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Filter discovered workflows to executable + policy-safe defaults.

        JVNAUTOSCI-1088:
        - Non-executable discovered concepts are excluded from selector context
          unless explicitly overridden.
        - Policy-safe defaults to "registered workflow concept ID" so the
          selector only routes into workflow IDs known to this process.
        """

        allow_non_executable = self._env_flag_enabled(
            "VON_WORKFLOW_SELECTOR_ALLOW_NON_EXECUTABLE"
        )
        allow_policy_unsafe = self._env_flag_enabled(
            "VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE"
        )
        registry_ids = {str(item) for item in self._workflow_registry.all_workflow_ids()}

        included: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for candidate in self._extract_discovery_candidates(workflow_discovery_result):
            concept_id = str(candidate.get("concept_id") or "").strip()
            if not concept_id:
                continue

            is_policy_safe = concept_id in registry_ids
            raw_reason = candidate.get("executability_reason")
            raw_is_executable = candidate.get("is_executable")
            if raw_reason is None and raw_is_executable is None:
                # Legacy discovery payloads did not expose executability metadata.
                # Use registry membership as a conservative executable default.
                is_executable = is_policy_safe
                reason = "executable_now" if is_executable else "graph_incomplete"
            else:
                reason = str(raw_reason or "non_executable_design_artifact").strip()
                is_executable = bool(
                    raw_is_executable or reason == "executable_now"
                )

            item = dict(candidate)
            item["concept_id"] = concept_id
            item["is_executable"] = is_executable
            item["executability_reason"] = reason
            item["is_policy_safe"] = is_policy_safe
            item["routing_eligible"] = True
            item["routing_exclusion_reason"] = None

            if not is_executable and not allow_non_executable:
                item["routing_eligible"] = False
                item["routing_exclusion_reason"] = f"non_executable:{reason}"
                excluded.append(item)
                continue
            if not is_policy_safe and not allow_policy_unsafe:
                item["routing_eligible"] = False
                item["routing_exclusion_reason"] = "policy_unsafe_not_registered"
                excluded.append(item)
                continue

            included.append(item)

        return included, excluded

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
        conversation_session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        workflow_discovery_result: Mapping[str, Any] | None = None,
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
                from ...workflows.workflow_definition_identity_service import (
                    build_workflow_definition_identity,
                )

                trace = WorkflowExecutionTrace(workflow_id="#V#chat_assistant_workflow")
                trace.user_namespace = user_namespace
                try:
                    registration = self._workflow_registry.get_registration(
                        CHAT_ASSISTANT_WORKFLOW_ID
                    )
                    definition = (
                        registration.definition
                        if registration is not None
                        else self._workflow_registry.get(CHAT_ASSISTANT_WORKFLOW_ID)
                    )
                    source = (
                        str(getattr(registration, "source", "") or "").strip()
                        if registration is not None
                        else "unknown"
                    ) or "unknown"
                    trace.metadata["workflow_definition_identity"] = (
                        build_workflow_definition_identity(
                            workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
                            source=source,
                            definition=definition,
                            authoritative_definition=(
                                definition if source.lower() == "vontology" else None
                            ),
                        )
                    )
                except Exception:
                    pass
                trace_store_fn = insert_workflow_execution_trace
            except Exception:
                trace_enabled = False
                trace = None
                trace_store_fn = None

        def _persist_trace(*, status: str, error: str | None = None) -> None:
            try:
                payload: dict[str, Any] = {
                    "status": "orchestrator_end",
                    "stage": "orchestrator_end",
                    "orchestrator_status": status,
                }
                if isinstance(error, str) and error.strip():
                    payload["error"] = error
                _emit_progress_local(payload)
            except Exception:
                pass

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

        _emit_progress_local(
            {
                "status": "orchestrator_start",
                "stage": "orchestrator_start",
            }
        )

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
                    emit_progress=_emit_progress_local,
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

        def _maybe_append_completion_claim_validation(
            response_text: str,
            *,
            tool_invocations_for_validation: Sequence[Mapping[str, Any]] = (),
            tool_messages_for_validation: Sequence[Mapping[str, Any]] = (),
        ) -> str:
            return self._apply_completion_claim_validation(
                response_text,
                tool_invocations=tool_invocations_for_validation,
                tool_messages=tool_messages_for_validation,
                aux_log=aux_llm_calls,
            )

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
                emit_progress=_emit_progress_local,
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
            response_text = _maybe_append_completion_claim_validation(response_text)
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

        preflight = self._build_ontology_preflight(
            prompt, preferred_language, context=context
        )
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

        # ----------------------------------------------------------------
        # JVNAUTOSCI-825 + 922: Workflow selection for all turns.
        #
        # The selector is the primary routing mechanism — it decides
        # whether to run tool-calling, narration, a discovered workflow,
        # or a direct plain response (no tool overhead).  Enabled by
        # default; disable with VON_CHAT_WORKFLOW_SELECTOR_ENABLED=0.
        #
        # Discovery results from discover_workflows_for_turn() are piped
        # into the selector so the classifier can route to dynamically
        # discovered Vontology workflows.
        # ----------------------------------------------------------------
        discovered_matches: list[dict[str, Any]] = []
        excluded_discovered_matches: list[dict[str, Any]] = []
        if isinstance(workflow_discovery_result, Mapping):
            discovered_matches, excluded_discovered_matches = (
                self._prepare_selector_discovered_matches(workflow_discovery_result)
            )

        routing_info: WorkflowRoutingInfo | None = None

        if user_namespace and self._workflow_selector.enabled():
            selector_start = time.perf_counter()
            try:
                classifier_model = _model_for_stage("classifier")
                selector_selection = self._workflow_selector.select_workflow(
                    llm_client=llm_client,
                    model=classifier_model,
                    turn_text=prompt,
                    discovered_workflows=discovered_matches or None,
                )
                routing_duration_ms = (time.perf_counter() - selector_start) * 1000.0
                if selector_selection.workflow_id:
                    selected_workflow_id = selector_selection.workflow_id
                routing_info = WorkflowRoutingInfo(
                    workflow_id=selector_selection.workflow_id,
                    verdict=selector_selection.verdict,
                    prompt_id=selector_selection.prompt_id,
                    discovered_workflow_ids=selector_selection.discovered_workflow_ids,
                    routing_duration_ms=routing_duration_ms,
                    source="selector",
                )
                aux_llm_calls.append(
                    {
                        "type": "workflow_selector",
                        "workflow_id": selector_selection.workflow_id,
                        "verdict": selector_selection.verdict,
                        "prompt_id": selector_selection.prompt_id,
                        "discovered_workflow_ids": list(
                            selector_selection.discovered_workflow_ids
                        ),
                        "discovery_candidate_count": len(discovered_matches)
                        + len(excluded_discovered_matches),
                        "discovery_excluded_count": len(excluded_discovered_matches),
                        "routing_duration_ms": routing_duration_ms,
                    }
                )
                if trace_enabled and trace is not None:
                    trace.metadata["workflow_selector"] = {
                        "workflow_id": selector_selection.workflow_id,
                        "verdict": selector_selection.verdict,
                        "prompt_id": selector_selection.prompt_id,
                        "discovered_workflow_ids": list(
                            selector_selection.discovered_workflow_ids
                        ),
                        "discovery_candidate_count": len(discovered_matches)
                        + len(excluded_discovered_matches),
                        "discovery_excluded_count": len(excluded_discovered_matches),
                        "excluded_discovered_workflow_ids": [
                            str(item.get("concept_id"))
                            for item in excluded_discovered_matches
                            if isinstance(item.get("concept_id"), str)
                        ],
                        "routing_duration_ms": routing_duration_ms,
                    }
            except Exception:
                selected_workflow_id = CHAT_ASSISTANT_WORKFLOW_ID
                routing_info = WorkflowRoutingInfo(
                    workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
                    verdict="fallback",
                    prompt_id=None,
                    discovered_workflow_ids=(),
                    source="default",
                )

        _STATIC_SELECTOR_VERDICTS = frozenset(
            {"plain_response", "tool_seeking", "summarisation", "narration", "fallback"}
        )
        _TOOL_PIPELINE_ACTION_IDS = frozenset(
            {
                "tool_calling.plan",
                "tool_calling.validate",
                "tool_calling.execute",
                "tool_calling.backfill",
            }
        )
        _NARRATION_ACTION_IDS = frozenset(
            {
                "narration.classify",
                "narration.select_prompts",
                "narration.render",
                "narration.emit_audio",
            }
        )
        selected_workflow_id_text = (
            selected_workflow_id.strip()
            if isinstance(selected_workflow_id, str) and selected_workflow_id.strip()
            else None
        )
        selector_verdict = (
            routing_info.verdict.strip().lower()
            if isinstance(routing_info, WorkflowRoutingInfo)
            and isinstance(routing_info.verdict, str)
            and routing_info.verdict.strip()
            else ""
        )
        selector_requests_narration = selector_verdict == "narration"
        selector_requests_custom_workflow = bool(
            selector_verdict
            and selector_verdict not in _STATIC_SELECTOR_VERDICTS
            and selected_workflow_id_text
            and selected_workflow_id_text.lower() == selector_verdict
        )

        def _workflow_action_ids(workflow_id: str | None) -> set[str]:
            if not isinstance(workflow_id, str) or not workflow_id.strip():
                return set()
            workflow_def = self._workflow_registry.get(workflow_id.strip())
            if workflow_def is None:
                return set()
            action_ids: set[str] = set()
            for state in workflow_def.states.values():
                for action in state.actions:
                    action_id = str(action.action_id or "").strip()
                    if action_id:
                        action_ids.add(action_id)
            return action_ids

        def _workflow_matches_action_contract(
            workflow_id: str | None,
            *,
            required_action_ids: frozenset[str],
        ) -> bool:
            action_ids = _workflow_action_ids(workflow_id)
            return bool(action_ids) and required_action_ids.issubset(action_ids)

        def _resolve_workflow_id_for_action_contract(
            *,
            required_action_ids: frozenset[str],
            preferred_workflow_id: str | None = None,
        ) -> str | None:
            preferred = (
                preferred_workflow_id.strip()
                if isinstance(preferred_workflow_id, str)
                and preferred_workflow_id.strip()
                else None
            )
            if preferred and _workflow_matches_action_contract(
                preferred, required_action_ids=required_action_ids
            ):
                return preferred
            for workflow_id in self._workflow_registry.all_workflow_ids():
                candidate = str(workflow_id).strip()
                if not candidate:
                    continue
                if _workflow_matches_action_contract(
                    candidate, required_action_ids=required_action_ids
                ):
                    return candidate
            return None

        # Feature-flagged step toward ontology-driven render planning:
        # resolve whether narration should be part of the response rendering.
        # Enabled by default; set VON_RENDERER_APPLICABILITY_ROUTING_ENABLE=0
        # to disable and preserve legacy behaviour.
        renderer_routing_enabled = self._env_flag_enabled(
            "VON_RENDERER_APPLICABILITY_ROUTING_ENABLE",
            default="1",
        )
        renderer_definition_concept_ids = self._env_csv_values(
            "VON_RENDERER_APPLICABILITY_DEFINITION_IDS"
        )
        renderer_definition_source = "environment"
        bootstrap_default_renderer_ids_enabled = self._env_flag_enabled(
            "VON_RENDERER_APPLICABILITY_BOOTSTRAP_DEFAULTS_ENABLE",
            default="1",
        )
        if (
            not renderer_definition_concept_ids
            and bootstrap_default_renderer_ids_enabled
        ):
            try:
                from ...services.renderer_applicability_vontology_service import (
                    canonical_renderer_profile_concept_ids,
                )

                canonical_renderer_ids = canonical_renderer_profile_concept_ids()
            except Exception:
                canonical_renderer_ids = ()
            if canonical_renderer_ids:
                renderer_definition_concept_ids = tuple(canonical_renderer_ids)
                renderer_definition_source = "canonical_bootstrap_defaults"
        renderer_preferred_modalities = self._env_csv_values(
            "VON_RENDERER_APPLICABILITY_PREFERRED_MODALITIES"
        )
        if not renderer_preferred_modalities:
            renderer_preferred_modalities = ("narrated_audio",)
        renderer_allow_multimodal = self._env_flag_enabled(
            "VON_RENDERER_APPLICABILITY_ALLOW_MULTIMODAL",
            default="1",
        )
        renderer_render_plan: dict[str, Any] | None = None

        def _iter_renderer_invocation_payloads(
            tool_invocations: Sequence[Mapping[str, Any]],
        ) -> Sequence[Mapping[str, Any]]:
            payloads: list[Mapping[str, Any]] = []
            for invocation in tool_invocations:
                if not isinstance(invocation, Mapping):
                    continue
                for field in ("effective_payload", "payload"):
                    payload = invocation.get(field)
                    if isinstance(payload, Mapping):
                        payloads.append(payload)
            return payloads

        def _extract_renderer_concept_candidates(
            tool_invocations: Sequence[Mapping[str, Any]],
        ) -> list[str]:
            candidate_keys = (
                "concept_id",
                "source_id",
                "target_id",
                "task_concept_id",
            )
            candidate_list_keys = (
                "concept_ids",
                "related_concept_ids",
            )
            blocked_ids = {
                user_concept_id.strip()
                if isinstance(user_concept_id, str) and user_concept_id.strip()
                else None,
                org_concept_id.strip()
                if isinstance(org_concept_id, str) and org_concept_id.strip()
                else None,
            }
            concept_ids: list[str] = []
            seen_ids: set[str] = set()
            for payload in _iter_renderer_invocation_payloads(tool_invocations):
                for key in candidate_keys:
                    raw_value = payload.get(key)
                    if not isinstance(raw_value, str):
                        continue
                    concept_id = raw_value.strip()
                    if not self._looks_like_concept_id(concept_id):
                        continue
                    if concept_id in blocked_ids:
                        continue
                    if concept_id in seen_ids:
                        continue
                    seen_ids.add(concept_id)
                    concept_ids.append(concept_id)
                for key in candidate_list_keys:
                    raw_values = payload.get(key)
                    if not isinstance(raw_values, (list, tuple)):
                        continue
                    for item in raw_values:
                        if not isinstance(item, str):
                            continue
                        concept_id = item.strip()
                        if not self._looks_like_concept_id(concept_id):
                            continue
                        if concept_id in blocked_ids:
                            continue
                        if concept_id in seen_ids:
                            continue
                        seen_ids.add(concept_id)
                        concept_ids.append(concept_id)
            return concept_ids

        def _extract_renderer_predicate_hints(
            tool_invocations: Sequence[Mapping[str, Any]],
        ) -> list[str]:
            predicate_hints: list[str] = []
            seen_hints: set[str] = set()

            def _add_hint(raw_value: Any) -> None:
                if not isinstance(raw_value, str):
                    return
                text = raw_value.strip()
                if not text or text in seen_hints:
                    return
                seen_hints.add(text)
                predicate_hints.append(text)

            for payload in _iter_renderer_invocation_payloads(tool_invocations):
                _add_hint(payload.get("predicate"))
                existing_hints = payload.get("present_predicates")
                if isinstance(existing_hints, (list, tuple)):
                    for item in existing_hints:
                        _add_hint(item)

            return predicate_hints[:40]

        def _iter_renderer_tool_result_payloads(
            tool_messages: Sequence[Mapping[str, Any]],
        ) -> Sequence[tuple[str, Mapping[str, Any]]]:
            """Yield successful tool result payload mappings from tool messages.

            Tool execution stores results as JSON strings via _format_tool_result.
            Parse those payloads here so render planning can use concrete tool
            outputs (tasks/extents) instead of only invocation inputs.
            """
            payloads: list[tuple[str, Mapping[str, Any]]] = []
            for message in tool_messages:
                if not isinstance(message, Mapping):
                    continue
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                try:
                    parsed = json.loads(content)
                except Exception:
                    continue
                if not isinstance(parsed, Mapping):
                    continue
                status = str(parsed.get("status") or "").strip().lower()
                if status != "ok":
                    continue
                tool_name_raw = parsed.get("tool")
                payload = parsed.get("payload")
                if not isinstance(tool_name_raw, str) or not tool_name_raw.strip():
                    continue
                if not isinstance(payload, Mapping):
                    continue
                payloads.append((tool_name_raw.strip().lower(), payload))
            return payloads

        def _first_text(*values: Any) -> str | None:
            for value in values:
                if not isinstance(value, str):
                    continue
                cleaned = value.strip()
                if cleaned:
                    return cleaned
            return None

        def _mapping_list(raw_value: Any, *, limit: int = 200) -> list[Mapping[str, Any]]:
            if not isinstance(raw_value, list):
                return []
            rows: list[Mapping[str, Any]] = []
            for item in raw_value:
                if not isinstance(item, Mapping):
                    continue
                rows.append(item)
                if len(rows) >= limit:
                    break
            return rows

        _renderer_concept_id_pattern = re.compile(r"^#V#[A-Za-z0-9._-]+$")
        _renderer_jira_issue_pattern = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

        def _collect_renderer_task_links(*values: Any) -> list[dict[str, str]]:
            links: list[dict[str, str]] = []
            seen_targets: set[tuple[str, str]] = set()

            def _add_link(link_type: str, target_id: str) -> None:
                target = target_id.strip()
                if not target:
                    return
                key = (link_type, target)
                if key in seen_targets:
                    return
                seen_targets.add(key)
                entry: dict[str, str] = {
                    "link_type": link_type,
                    "target_id": target,
                    "label": target,
                }
                if link_type == "jira_issue":
                    entry["href"] = (
                        f"https://naoinstitute.atlassian.net/browse/{target}"
                    )
                links.append(entry)

            def _walk(value: Any, path: tuple[str, ...], depth: int) -> None:
                if depth > 4 or len(links) >= 12:
                    return
                if isinstance(value, Mapping):
                    for raw_key, nested in value.items():
                        key_text = str(raw_key or "").strip().lower()
                        if isinstance(nested, str):
                            candidate = nested.strip()
                            if not candidate:
                                continue
                            path_tokens = " ".join(path + (key_text,))
                            if _renderer_concept_id_pattern.match(candidate) and (
                                "task" in path_tokens or "task" in candidate.lower()
                            ):
                                _add_link("von_task", candidate)
                                continue
                            if _renderer_jira_issue_pattern.match(candidate):
                                _add_link("jira_issue", candidate)
                            continue
                        if isinstance(nested, (Mapping, list, tuple)):
                            _walk(nested, path + (key_text,), depth + 1)
                elif isinstance(value, (list, tuple)):
                    for nested in value:
                        if isinstance(nested, (Mapping, list, tuple)):
                            _walk(nested, path, depth + 1)
                        elif isinstance(nested, str):
                            candidate = nested.strip()
                            if _renderer_jira_issue_pattern.match(candidate):
                                _add_link("jira_issue", candidate)
                            elif _renderer_concept_id_pattern.match(candidate) and (
                                "task" in candidate.lower()
                            ):
                                _add_link("von_task", candidate)

            for value in values:
                _walk(value, tuple(), 0)

            return links

        def _extract_renderer_task_entries(
            *,
            tool_messages: Sequence[Mapping[str, Any]] = (),
            limit: int = 200,
        ) -> tuple[list[dict[str, Any]], list[str]]:
            """Extract canonical task entries from task-oriented tool payloads."""

            task_tool_names = {"task_list", "task_search", "list_my_tasks"}
            source_tools: list[str] = []
            seen_source_tools: set[str] = set()
            task_entries: list[dict[str, Any]] = []
            seen_task_ids: set[str] = set()

            for tool_name, payload in _iter_renderer_tool_result_payloads(tool_messages):
                if tool_name not in task_tool_names:
                    continue

                if tool_name not in seen_source_tools:
                    seen_source_tools.add(tool_name)
                    source_tools.append(tool_name)

                task_rows = _mapping_list(payload.get("tasks") or payload.get("results"))
                for index, task in enumerate(task_rows, start=1):
                    task_id = _first_text(
                        task.get("task_concept_id"),
                        task.get("task_id"),
                        task.get("concept_id"),
                        task.get("id"),
                    )
                    if not task_id:
                        task_id = f"{tool_name}_task_{index}"
                    if task_id in seen_task_ids:
                        continue
                    seen_task_ids.add(task_id)

                    title = _first_text(
                        task.get("title"),
                        task.get("task_title"),
                        task.get("name"),
                    ) or task_id
                    task_entry: dict[str, Any] = {
                        "task_id": task_id,
                        "title": title,
                    }
                    status = _first_text(task.get("status"))
                    if status:
                        task_entry["status"] = status.lower()
                    priority = _first_text(task.get("priority"))
                    if priority:
                        task_entry["priority"] = priority
                    start_at = _first_text(
                        task.get("start_at"),
                        task.get("starts_at"),
                        task.get("start_date"),
                        task.get("scheduled_start_at"),
                    )
                    end_at = _first_text(
                        task.get("end_at"),
                        task.get("ends_at"),
                        task.get("due_date"),
                        task.get("due_at"),
                        task.get("target_date"),
                        task.get("milestone_date"),
                    )
                    due_date = _first_text(
                        task.get("due_date"),
                        task.get("due_at"),
                        task.get("target_date"),
                        task.get("milestone_date"),
                        end_at,
                    )
                    if due_date:
                        task_entry["due_date"] = due_date
                    if start_at:
                        task_entry["start_at"] = start_at
                    if end_at:
                        task_entry["end_at"] = end_at
                    assignee = _first_text(
                        task.get("assignee_concept_id"),
                        task.get("assignee_id"),
                        task.get("assignee"),
                    )
                    if assignee:
                        task_entry["assignee"] = assignee
                    timezone = _first_text(
                        task.get("timezone"),
                        task.get("time_zone"),
                        task.get("tz"),
                    )
                    if timezone:
                        task_entry["timezone"] = timezone
                    all_day = task.get("all_day")
                    if isinstance(all_day, bool):
                        task_entry["all_day"] = all_day
                    description = _first_text(
                        task.get("description"),
                        task.get("summary"),
                    )
                    if description:
                        task_entry["description"] = description

                    task_links = _collect_renderer_task_links(task)
                    if task_links:
                        task_entry["task_links"] = task_links

                    task_entries.append(task_entry)
                    if len(task_entries) >= limit:
                        break
                if len(task_entries) >= limit:
                    break

            return task_entries, source_tools

        def _extract_renderer_screen_table_record_sets(
            *,
            tool_messages: Sequence[Mapping[str, Any]] = (),
        ) -> list[dict[str, Any]]:
            """Derive canonical record sets for screen tables from tool outputs."""

            def _task_record_set() -> dict[str, Any] | None:
                task_tool_names = {"task_list", "task_search", "list_my_tasks"}
                for tool_name, payload in _iter_renderer_tool_result_payloads(
                    tool_messages
                ):
                    if tool_name not in task_tool_names:
                        continue

                    tasks = _mapping_list(payload.get("tasks") or payload.get("results"))
                    if not tasks:
                        continue

                    records: list[dict[str, Any]] = []
                    for index, task in enumerate(tasks, start=1):
                        task_id = _first_text(
                            task.get("task_concept_id"),
                            task.get("task_id"),
                            task.get("concept_id"),
                            task.get("id"),
                        )
                        task_name = _first_text(
                            task.get("title"),
                            task.get("task_title"),
                            task.get("name"),
                        ) or task_id or f"Task {index}"
                        status = _first_text(task.get("status")) or ""
                        priority = _first_text(task.get("priority")) or ""
                        due_date = _first_text(task.get("due_date")) or ""
                        assignee = _first_text(
                            task.get("assignee_concept_id"),
                            task.get("assignee_id"),
                            task.get("assignee"),
                        ) or ""

                        record: dict[str, Any] = {
                            "task_id": task_id or f"task_{index}",
                            "task_name": task_name,
                            "status": status,
                            "priority": priority,
                            "due_date": due_date,
                            "assignee": assignee,
                            "source": {
                                "source_tool": tool_name,
                            },
                        }
                        if task_id:
                            record["source"]["task_concept_id"] = task_id
                        records.append(record)

                    if not records:
                        continue

                    return {
                        "element_id": "screen_task_table",
                        "intent": "structured_tabular_view",
                        "records": records,
                        "columns": [
                            {
                                "column_id": "task_name",
                                "label": "Task",
                                "source_key": "task_name",
                                "data_type": "text",
                            },
                            {
                                "column_id": "status",
                                "label": "Status",
                                "source_key": "status",
                                "data_type": "text",
                            },
                            {
                                "column_id": "priority",
                                "label": "Priority",
                                "source_key": "priority",
                                "data_type": "text",
                            },
                            {
                                "column_id": "due_date",
                                "label": "Due",
                                "source_key": "due_date",
                                "data_type": "text",
                            },
                        ],
                        "row_id_field": "task_id",
                        "row_provenance_field": "source",
                        "default_sort_column_id": "task_name",
                        "provenance": {
                            "source": "tool_result_record_set",
                            "source_tool": tool_name,
                            "record_family": "tasks",
                        },
                    }
                return None

            def _predicate_extent_record_set() -> dict[str, Any] | None:
                for tool_name, payload in _iter_renderer_tool_result_payloads(
                    tool_messages
                ):
                    if tool_name != "get_predicate_extent":
                        continue

                    extent_rows = _mapping_list(payload.get("extent"))
                    if not extent_rows:
                        continue
                    predicate_concept_id = _first_text(
                        payload.get("concept_id"),
                        payload.get("predicate_concept_id"),
                    )

                    records: list[dict[str, Any]] = []
                    for index, item in enumerate(extent_rows, start=1):
                        subject = _first_text(item.get("subject")) or ""
                        predicate = _first_text(item.get("predicate")) or (
                            predicate_concept_id or ""
                        )
                        object_raw = item.get("object")
                        object_value = (
                            object_raw.strip()
                            if isinstance(object_raw, str)
                            else (str(object_raw) if object_raw is not None else "")
                        )
                        source_value = _first_text(item.get("source")) or ""
                        assertion_id = _first_text(
                            item.get("assertion_id"),
                            item.get("relation_id"),
                            item.get("id"),
                        ) or f"extent_{index}"
                        if not any((subject, predicate, object_value, source_value)):
                            continue

                        assertion_meta: dict[str, Any] = {
                            "source_tool": tool_name,
                        }
                        if predicate_concept_id:
                            assertion_meta["predicate_concept_id"] = predicate_concept_id
                        if source_value:
                            assertion_meta["source"] = source_value
                        created_at = item.get("created_at")
                        updated_at = item.get("updated_at")
                        if created_at is not None:
                            assertion_meta["created_at"] = str(created_at)
                        if updated_at is not None:
                            assertion_meta["updated_at"] = str(updated_at)

                        records.append(
                            {
                                "assertion_id": assertion_id,
                                "subject": subject,
                                "predicate": predicate,
                                "object": object_value,
                                "source": source_value,
                                "assertion_meta": assertion_meta,
                            }
                        )

                    if not records:
                        continue

                    return {
                        "element_id": "screen_predicate_extent_table",
                        "intent": "structured_tabular_view",
                        "records": records,
                        "columns": [
                            {
                                "column_id": "subject",
                                "label": "Subject",
                                "source_key": "subject",
                                "data_type": "text",
                            },
                            {
                                "column_id": "predicate",
                                "label": "Predicate",
                                "source_key": "predicate",
                                "data_type": "text",
                            },
                            {
                                "column_id": "object",
                                "label": "Object",
                                "source_key": "object",
                                "data_type": "text",
                            },
                            {
                                "column_id": "source",
                                "label": "Source",
                                "source_key": "source",
                                "data_type": "text",
                            },
                        ],
                        "row_id_field": "assertion_id",
                        "row_provenance_field": "assertion_meta",
                        "default_sort_column_id": "subject",
                        "pagination_enabled": False,
                        "provenance": {
                            "source": "tool_result_record_set",
                            "source_tool": tool_name,
                            "record_family": "predicate_extent",
                        },
                    }
                return None

            record_sets: list[dict[str, Any]] = []
            task_records = _task_record_set()
            if isinstance(task_records, dict):
                record_sets.append(task_records)
            predicate_records = _predicate_extent_record_set()
            if isinstance(predicate_records, dict):
                record_sets.append(predicate_records)
            return record_sets

        def _extract_renderer_screen_workflow_elements(
            *,
            tool_messages: Sequence[Mapping[str, Any]] = (),
        ) -> list[dict[str, Any]]:
            """Derive workflow view payloads from workflow tool outputs.

            The renderer may only have partial workflow graph data (for example,
            status snapshots without edge topology). We therefore emit a
            deterministic list-style workflow payload that still includes any
            discoverable Von task concept links and Jira issue links.
            """

            tool_payloads = list(_iter_renderer_tool_result_payloads(tool_messages))
            if not tool_payloads:
                return []

            workflow_detail_by_instance_id: dict[str, Mapping[str, Any]] = {}
            workflow_list_payloads: list[Mapping[str, Any]] = []
            source_tools: list[str] = []
            seen_source_tools: set[str] = set()

            for tool_name, payload in tool_payloads:
                if tool_name in {"workflow_list_instances", "workflow_get_instance"}:
                    if tool_name not in seen_source_tools:
                        seen_source_tools.add(tool_name)
                        source_tools.append(tool_name)

                if tool_name == "workflow_get_instance":
                    instance_id = _first_text(payload.get("instance_id"))
                    if instance_id:
                        workflow_detail_by_instance_id[instance_id] = payload
                    continue
                if tool_name == "workflow_list_instances":
                    workflow_list_payloads.append(payload)

            workflow_rows: list[Mapping[str, Any]] = []
            for payload in workflow_list_payloads:
                workflow_rows.extend(_mapping_list(payload.get("instances"), limit=200))

            if not workflow_rows and workflow_detail_by_instance_id:
                workflow_rows = list(workflow_detail_by_instance_id.values())

            if not workflow_rows:
                return []

            nodes: list[dict[str, Any]] = []
            seen_node_ids: set[str] = set()
            for index, workflow_row in enumerate(workflow_rows, start=1):
                detail = None
                if isinstance(workflow_row, Mapping):
                    instance_id = _first_text(workflow_row.get("instance_id"))
                    if instance_id:
                        detail = workflow_detail_by_instance_id.get(instance_id)
                else:
                    continue

                instance_id = _first_text(
                    workflow_row.get("instance_id"),
                    detail.get("instance_id") if isinstance(detail, Mapping) else None,
                )
                workflow_id = _first_text(
                    workflow_row.get("workflow_id"),
                    detail.get("workflow_id") if isinstance(detail, Mapping) else None,
                )
                node_id = instance_id or workflow_id or f"workflow_node_{index}"
                if node_id in seen_node_ids:
                    continue
                seen_node_ids.add(node_id)

                status = (
                    _first_text(
                        workflow_row.get("status"),
                        detail.get("status") if isinstance(detail, Mapping) else None,
                    )
                    or "unknown"
                ).lower()
                current_state = _first_text(
                    workflow_row.get("current_state"),
                    detail.get("current_state") if isinstance(detail, Mapping) else None,
                ) or ""

                progress_raw = workflow_row.get("progress")
                if isinstance(progress_raw, Mapping):
                    progress_map: Mapping[str, Any] = progress_raw
                elif isinstance(detail, Mapping) and isinstance(
                    detail.get("progress"), Mapping
                ):
                    progress_map = cast(Mapping[str, Any], detail.get("progress"))
                else:
                    progress_map = {}
                progress_current = (
                    progress_map.get("current")
                    if isinstance(progress_map.get("current"), (int, float))
                    else None
                )
                progress_total = (
                    progress_map.get("total")
                    if isinstance(progress_map.get("total"), (int, float))
                    else None
                )
                progress_message = _first_text(progress_map.get("message"))
                step_index = workflow_row.get("step_index")
                if not isinstance(step_index, (int, float)):
                    if isinstance(detail, Mapping) and isinstance(
                        detail.get("step_index"), (int, float)
                    ):
                        step_index = detail.get("step_index")
                    else:
                        step_index = None

                progress_bits: list[str] = []
                if isinstance(progress_current, (int, float)) and isinstance(
                    progress_total, (int, float)
                ):
                    progress_bits.append(
                        f"Step {int(progress_current)}/{int(progress_total)}"
                    )
                elif isinstance(step_index, (int, float)):
                    progress_bits.append(f"Step {int(step_index)}")
                if progress_message:
                    progress_bits.append(progress_message)
                elif current_state:
                    progress_bits.append(current_state)

                task_links = _collect_renderer_task_links(
                    workflow_row,
                    detail if isinstance(detail, Mapping) else None,
                    detail.get("inputs") if isinstance(detail, Mapping) else None,
                    detail.get("outputs") if isinstance(detail, Mapping) else None,
                    detail.get("workflow_data") if isinstance(detail, Mapping) else None,
                )

                label = workflow_id or instance_id or f"Workflow {index}"
                node_payload: dict[str, Any] = {
                    "node_id": node_id,
                    "label": label,
                    "status": status,
                }
                if workflow_id:
                    node_payload["workflow_id"] = workflow_id
                if instance_id:
                    node_payload["instance_id"] = instance_id
                if current_state:
                    node_payload["state"] = current_state
                if progress_bits:
                    node_payload["progress_label"] = " · ".join(progress_bits)
                if task_links:
                    node_payload["task_links"] = task_links

                nodes.append(node_payload)
                if len(nodes) >= 50:
                    break

            if not nodes:
                return []

            return [
                {
                    "element_id": "screen_workflow_view",
                    "intent": "structured_workflow_view",
                    "payload": {
                        "layout": "list",
                        "nodes": nodes,
                        "edges": [],
                    },
                    "constraints": {
                        "supports_node_links": True,
                        "supports_task_navigation": True,
                    },
                    "provenance": {
                        "source": "tool_result_workflow_view",
                        "source_tools": source_tools,
                        "record_family": "workflow_instances",
                    },
                }
            ]

        def _extract_renderer_screen_task_view_elements(
            *,
            tool_messages: Sequence[Mapping[str, Any]] = (),
        ) -> list[dict[str, Any]]:
            """Derive task view payloads from task-oriented tool outputs."""

            tasks_payload, source_tools = _extract_renderer_task_entries(
                tool_messages=tool_messages,
                limit=200,
            )

            if not tasks_payload:
                return []

            return [
                {
                    "element_id": "screen_task_view",
                    "intent": "structured_task_view",
                    "payload": {"tasks": tasks_payload},
                    "constraints": {
                        "supports_task_links": True,
                        "supports_status_badges": True,
                    },
                    "provenance": {
                        "source": "tool_result_task_view",
                        "source_tools": source_tools,
                        "record_family": "tasks",
                    },
                }
            ]

        def _extract_renderer_screen_kanban_elements(
            *,
            tool_messages: Sequence[Mapping[str, Any]] = (),
        ) -> list[dict[str, Any]]:
            """Derive kanban payloads from task and workflow tool outputs."""

            tasks_payload, source_tools = _extract_renderer_task_entries(
                tool_messages=tool_messages,
                limit=200,
            )
            source_tool_set: set[str] = set(source_tools)

            seen_card_ids: set[str] = set()
            cards: list[dict[str, Any]] = []

            def _normalise_column_id(raw_status: Any) -> str:
                if isinstance(raw_status, str) and raw_status.strip():
                    base = raw_status.strip().lower()
                else:
                    base = "uncategorised"
                return re.sub(r"[^a-z0-9_]+", "_", base).strip("_") or "uncategorised"

            def _append_task_card(task_row: Mapping[str, Any], fallback_card_id: str) -> None:
                card_id = _first_text(task_row.get("task_id"), task_row.get("task_concept_id"))
                if not card_id:
                    card_id = fallback_card_id
                if card_id in seen_card_ids:
                    return
                seen_card_ids.add(card_id)

                title = _first_text(task_row.get("title")) or card_id
                column_id = _normalise_column_id(task_row.get("status"))
                card: dict[str, Any] = {
                    "card_id": card_id,
                    "title": title,
                    "column_id": column_id,
                }
                for field_name in ("priority", "assignee", "due_date", "description"):
                    field_value = _first_text(task_row.get(field_name))
                    if field_value:
                        card[field_name] = field_value

                task_links = _collect_renderer_task_links(task_row)
                if task_links:
                    card["task_links"] = task_links
                cards.append(card)

            for index, task in enumerate(tasks_payload, start=1):
                _append_task_card(task, f"task_card_{index}")
                if len(cards) >= 200:
                    break

            if len(cards) < 200:
                for tool_name, payload in _iter_renderer_tool_result_payloads(tool_messages):
                    workflow_rows: list[Mapping[str, Any]]
                    if tool_name == "workflow_list_instances":
                        workflow_rows = _mapping_list(payload.get("instances"), limit=200)
                    elif tool_name == "workflow_get_instance":
                        workflow_rows = [payload] if isinstance(payload, Mapping) else []
                    else:
                        continue

                    if tool_name not in source_tool_set:
                        source_tool_set.add(tool_name)
                        source_tools.append(tool_name)

                    for index, workflow_row in enumerate(workflow_rows, start=1):
                        instance_id = _first_text(workflow_row.get("instance_id"))
                        workflow_id = _first_text(workflow_row.get("workflow_id"))
                        card_id = (
                            instance_id
                            or workflow_id
                            or f"{tool_name}_workflow_card_{index}"
                        )
                        title = workflow_id or instance_id or f"Workflow {index}"
                        status = _first_text(workflow_row.get("status"))
                        current_state = _first_text(workflow_row.get("current_state"))
                        task_like_row: dict[str, Any] = {
                            "task_id": card_id,
                            "title": title,
                        }
                        if status:
                            task_like_row["status"] = status
                        if current_state:
                            task_like_row["description"] = current_state
                        task_links = _collect_renderer_task_links(workflow_row)
                        if task_links:
                            task_like_row["task_links"] = task_links
                        _append_task_card(task_like_row, card_id)
                        if len(cards) >= 200:
                            break
                    if len(cards) >= 200:
                        break

            if not cards:
                return []

            order_by_column_id: dict[str, int] = {
                "backlog": 10,
                "todo": 20,
                "pending": 30,
                "running": 40,
                "in_progress": 50,
                "blocked": 60,
                "in_review": 70,
                "done": 80,
                "completed": 90,
                "cancelled": 100,
                "failed": 110,
                "unknown": 120,
                "uncategorised": 130,
            }
            columns_map: dict[str, dict[str, Any]] = {}
            next_dynamic_order = 200
            for card in cards:
                column_id = str(card.get("column_id") or "").strip()
                if not column_id:
                    column_id = "uncategorised"
                    card["column_id"] = column_id
                if column_id in columns_map:
                    continue

                order = order_by_column_id.get(column_id)
                if order is None:
                    order = next_dynamic_order
                    next_dynamic_order += 1
                label = re.sub(r"_+", " ", column_id).strip().title() or "Uncategorised"
                columns_map[column_id] = {
                    "column_id": column_id,
                    "label": label,
                    "order": int(order),
                }

            ordered_columns = sorted(
                columns_map.values(),
                key=lambda item: (int(item.get("order", 0)), str(item.get("column_id", ""))),
            )

            return [
                {
                    "element_id": "screen_kanban_view",
                    "intent": "structured_kanban_view",
                    "payload": {
                        "columns": ordered_columns,
                        "cards": cards,
                    },
                    "constraints": {
                        "supports_column_grouping": True,
                        "supports_task_links": True,
                    },
                    "provenance": {
                        "source": "tool_result_kanban_view",
                        "source_tools": source_tools,
                        "record_family": "tasks_and_workflows",
                    },
                }
            ]

        def _extract_renderer_screen_timeline_elements(
            *,
            tool_messages: Sequence[Mapping[str, Any]] = (),
        ) -> list[dict[str, Any]]:
            """Derive timeline payloads from task/workflow history style tool outputs."""

            tool_payloads = list(_iter_renderer_tool_result_payloads(tool_messages))
            if not tool_payloads:
                return []

            source_tools: list[str] = []
            seen_source_tools: set[str] = set()
            timeline_items: list[dict[str, Any]] = []
            seen_item_ids: set[str] = set()

            def _register_source_tool(tool_name: str) -> None:
                if tool_name in seen_source_tools:
                    return
                seen_source_tools.add(tool_name)
                source_tools.append(tool_name)

            def _next_item_id(base: str) -> str:
                candidate = base
                suffix = 2
                while candidate in seen_item_ids:
                    candidate = f"{base}_{suffix}"
                    suffix += 1
                seen_item_ids.add(candidate)
                return candidate

            def _append_item(
                *,
                base_item_id: str,
                label: str,
                start_at: str | None = None,
                end_at: str | None = None,
                status: str | None = None,
                description: str | None = None,
                task_links: Sequence[dict[str, str]] | None = None,
            ) -> None:
                start_value = _first_text(start_at)
                end_value = _first_text(end_at)
                if not start_value and not end_value:
                    return
                item: dict[str, Any] = {
                    "item_id": _next_item_id(base_item_id),
                    "label": label,
                }
                if start_value:
                    item["start_at"] = start_value
                if end_value:
                    item["end_at"] = end_value
                status_value = _first_text(status)
                if status_value:
                    item["status"] = status_value.lower()
                description_value = _first_text(description)
                if description_value:
                    item["description"] = description_value
                if task_links:
                    item["task_links"] = list(task_links)
                timeline_items.append(item)

            def _event_label(raw_event_type: str) -> str:
                return re.sub(r"_+", " ", raw_event_type).strip() or "Task event"

            for tool_name, payload in tool_payloads:
                if tool_name in {"task_get_history", "task_list_worklog"}:
                    _register_source_tool(tool_name)
                if tool_name in {"workflow_list_instances", "workflow_get_instance"}:
                    _register_source_tool(tool_name)

                if tool_name == "task_get_history":
                    task_concept_id = _first_text(
                        payload.get("task_concept_id"),
                        payload.get("task_id"),
                    )
                    history_rows = _mapping_list(payload.get("history"), limit=400)
                    for index, row in enumerate(history_rows, start=1):
                        event_type = (
                            _first_text(row.get("event_type")) or "task_event"
                        ).lower()
                        base_item_id = _first_text(row.get("event_id"), row.get("id")) or (
                            f"task_history_{index}"
                        )
                        timestamp = _first_text(
                            row.get("timestamp"),
                            row.get("created_at"),
                            row.get("updated_at"),
                        )
                        details = (
                            cast(Mapping[str, Any], row.get("details"))
                            if isinstance(row.get("details"), Mapping)
                            else {}
                        )
                        status = _first_text(
                            row.get("status"),
                            details.get("to_status"),
                            details.get("status"),
                        )
                        description = _first_text(
                            details.get("comment"),
                            details.get("reason"),
                            details.get("summary"),
                        )
                        task_links = _collect_renderer_task_links(
                            {"task_concept_id": task_concept_id},
                            row,
                            details,
                        )
                        _append_item(
                            base_item_id=base_item_id,
                            label=_event_label(event_type),
                            start_at=timestamp,
                            status=status,
                            description=description,
                            task_links=task_links,
                        )
                    continue

                if tool_name == "task_list_worklog":
                    task_concept_id = _first_text(
                        payload.get("task_concept_id"),
                        payload.get("task_id"),
                    )
                    worklog_rows = _mapping_list(payload.get("worklog"), limit=200)
                    for index, row in enumerate(worklog_rows, start=1):
                        base_item_id = _first_text(
                            row.get("worklog_id"),
                            row.get("id"),
                        ) or f"task_worklog_{index}"
                        started_at = _first_text(
                            row.get("started_at"),
                            row.get("created_at"),
                            row.get("timestamp"),
                        )
                        time_spent = row.get("time_spent_minutes")
                        time_spent_label = (
                            f"{int(time_spent)} min"
                            if isinstance(time_spent, (int, float))
                            else None
                        )
                        description = _first_text(
                            row.get("comment"),
                            time_spent_label,
                        )
                        task_links = _collect_renderer_task_links(
                            {"task_concept_id": task_concept_id},
                            row,
                        )
                        _append_item(
                            base_item_id=base_item_id,
                            label="Worklog entry",
                            start_at=started_at,
                            description=description,
                            task_links=task_links,
                        )
                    continue

                if tool_name == "workflow_list_instances":
                    workflow_rows = _mapping_list(payload.get("instances"), limit=200)
                elif tool_name == "workflow_get_instance":
                    workflow_rows = [payload]
                else:
                    workflow_rows = []

                for index, row in enumerate(workflow_rows, start=1):
                    instance_id = _first_text(row.get("instance_id"))
                    workflow_id = _first_text(row.get("workflow_id"))
                    base_item_id = (
                        instance_id
                        or workflow_id
                        or f"{tool_name}_workflow_{index}"
                    )
                    start_at = _first_text(
                        row.get("created_at"),
                        row.get("started_at"),
                    )
                    end_at = _first_text(
                        row.get("completed_at"),
                        row.get("finished_at"),
                    )
                    if not start_at and not end_at:
                        start_at = _first_text(row.get("updated_at"))
                    label = workflow_id or instance_id or "Workflow event"
                    task_links = _collect_renderer_task_links(row)
                    _append_item(
                        base_item_id=base_item_id,
                        label=label,
                        start_at=start_at,
                        end_at=end_at,
                        status=_first_text(row.get("status")),
                        description=_first_text(row.get("current_state")),
                        task_links=task_links,
                    )

            if not timeline_items:
                return []

            return [
                {
                    "element_id": "screen_timeline_view",
                    "intent": "structured_timeline_view",
                    "payload": {"items": timeline_items[:200]},
                    "constraints": {
                        "supports_item_links": True,
                        "supports_relative_time": True,
                    },
                    "provenance": {
                        "source": "tool_result_timeline",
                        "source_tools": source_tools,
                        "record_family": "timeline_events",
                    },
                }
            ]

        def _extract_renderer_screen_calendar_elements(
            *,
            tool_messages: Sequence[Mapping[str, Any]] = (),
        ) -> list[dict[str, Any]]:
            """Derive calendar payloads from task/workflow timeline style outputs."""

            max_items = 240
            source_tools: list[str] = []
            seen_source_tools: set[str] = set()
            calendar_items: list[dict[str, Any]] = []
            seen_item_ids: set[str] = set()

            def _register_source_tool(tool_name: str) -> None:
                if tool_name in seen_source_tools:
                    return
                seen_source_tools.add(tool_name)
                source_tools.append(tool_name)

            def _next_item_id(base: str) -> str:
                candidate = base
                suffix = 2
                while candidate in seen_item_ids:
                    candidate = f"{base}_{suffix}"
                    suffix += 1
                seen_item_ids.add(candidate)
                return candidate

            def _is_date_only(raw_value: Any) -> bool:
                return bool(
                    isinstance(raw_value, str)
                    and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_value.strip())
                )

            def _extract_focus_date(raw_value: Any) -> str | None:
                if not isinstance(raw_value, str):
                    return None
                value = raw_value.strip()
                if not value:
                    return None
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    return value
                if re.match(r"^\d{4}-\d{2}-\d{2}[Tt\s]", value):
                    return value[:10]
                return None

            def _append_item(
                *,
                base_item_id: str,
                title: str,
                start_at: Any = None,
                end_at: Any = None,
                all_day: Any = None,
                status: Any = None,
                timezone: Any = None,
                description: Any = None,
                task_links: Sequence[dict[str, str]] | None = None,
            ) -> None:
                if len(calendar_items) >= max_items:
                    return

                start_value = _first_text(start_at)
                end_value = _first_text(end_at)
                if not start_value and not end_value:
                    return

                item: dict[str, Any] = {
                    "item_id": _next_item_id(base_item_id),
                    "title": title,
                }
                if start_value:
                    item["start_at"] = start_value
                if end_value:
                    item["end_at"] = end_value

                if isinstance(all_day, bool):
                    all_day_value = all_day
                else:
                    all_day_value = _is_date_only(start_value) and (
                        not end_value or _is_date_only(end_value)
                    )
                item["all_day"] = bool(all_day_value)

                status_value = _first_text(status)
                if status_value:
                    item["status"] = status_value.lower()

                timezone_value = _first_text(timezone)
                if timezone_value:
                    item["timezone"] = timezone_value

                description_value = _first_text(description)
                if description_value:
                    item["description"] = description_value

                if task_links:
                    item["task_links"] = list(task_links)

                calendar_items.append(item)

            timeline_elements = _extract_renderer_screen_timeline_elements(
                tool_messages=tool_messages
            )
            if timeline_elements:
                timeline_element = timeline_elements[0]
                timeline_payload = timeline_element.get("payload")
                if isinstance(timeline_payload, Mapping):
                    for timeline_item in _mapping_list(
                        timeline_payload.get("items"),
                        limit=max_items,
                    ):
                        item_id = _first_text(
                            timeline_item.get("item_id"),
                            timeline_item.get("id"),
                        ) or f"timeline_item_{len(calendar_items) + 1}"
                        title = _first_text(
                            timeline_item.get("label"),
                            timeline_item.get("title"),
                        ) or item_id
                        _append_item(
                            base_item_id=item_id,
                            title=title,
                            start_at=timeline_item.get("start_at"),
                            end_at=timeline_item.get("end_at"),
                            status=timeline_item.get("status"),
                            description=timeline_item.get("description"),
                            task_links=_collect_renderer_task_links(timeline_item),
                        )

                timeline_provenance_raw = timeline_element.get("provenance")
                timeline_provenance: Mapping[str, Any] = (
                    cast(Mapping[str, Any], timeline_provenance_raw)
                    if isinstance(timeline_provenance_raw, Mapping)
                    else {}
                )
                raw_timeline_source_tools = timeline_provenance.get("source_tools")
                if isinstance(raw_timeline_source_tools, Sequence) and not isinstance(
                    raw_timeline_source_tools,
                    (str, bytes, bytearray),
                ):
                    for raw_tool_name in raw_timeline_source_tools:
                        candidate = _first_text(raw_tool_name)
                        if candidate:
                            _register_source_tool(candidate)

            task_entries, task_source_tools = _extract_renderer_task_entries(
                tool_messages=tool_messages,
                limit=max_items,
            )
            for tool_name in task_source_tools:
                _register_source_tool(tool_name)

            for index, task_entry in enumerate(task_entries, start=1):
                task_id = _first_text(task_entry.get("task_id")) or f"task_{index}"
                title = _first_text(task_entry.get("title")) or task_id
                start_at = _first_text(
                    task_entry.get("start_at"),
                    task_entry.get("due_date"),
                    task_entry.get("end_at"),
                )
                end_at = _first_text(
                    task_entry.get("end_at"),
                    task_entry.get("due_date"),
                )
                _append_item(
                    base_item_id=task_id,
                    title=title,
                    start_at=start_at,
                    end_at=end_at,
                    all_day=task_entry.get("all_day"),
                    status=task_entry.get("status"),
                    timezone=task_entry.get("timezone"),
                    description=task_entry.get("description"),
                    task_links=_collect_renderer_task_links(task_entry),
                )
                if len(calendar_items) >= max_items:
                    break

            if not calendar_items:
                return []

            focus_date: str | None = None
            for item in calendar_items:
                focus_date = _extract_focus_date(item.get("start_at")) or _extract_focus_date(
                    item.get("end_at")
                )
                if focus_date:
                    break

            payload: dict[str, Any] = {
                "items": calendar_items[:max_items],
                "default_granularity": "month",
            }
            if focus_date:
                payload["focus_date"] = focus_date

            return [
                {
                    "element_id": "screen_calendar_view",
                    "intent": "structured_calendar_view",
                    "payload": payload,
                    "constraints": {
                        "supports_task_links": True,
                        "supports_granularity_switch": True,
                    },
                    "provenance": {
                        "source": "tool_result_calendar_view",
                        "source_tools": source_tools,
                        "record_family": "calendar_items",
                    },
                }
            ]

        def _extract_renderer_screen_document_elements(
            *,
            tool_messages: Sequence[Mapping[str, Any]] = (),
        ) -> list[dict[str, Any]]:
            """Derive document payloads from paper/notes/history style tool outputs."""

            max_documents = 12
            max_sections = 12
            source_tools: list[str] = []
            seen_source_tools: set[str] = set()
            documents: list[dict[str, Any]] = []
            seen_document_ids: set[str] = set()

            def _register_source_tool(tool_name: str) -> None:
                if tool_name in seen_source_tools:
                    return
                seen_source_tools.add(tool_name)
                source_tools.append(tool_name)

            def _next_document_id(base: str) -> str:
                candidate = base
                suffix = 2
                while candidate in seen_document_ids:
                    candidate = f"{base}_{suffix}"
                    suffix += 1
                seen_document_ids.add(candidate)
                return candidate

            def _safe_text(value: Any) -> str | None:
                if isinstance(value, str):
                    cleaned = value.strip()
                    return cleaned or None
                if value is None:
                    return None
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cleaned = str(value).strip()
                    return cleaned or None
                return None

            def _first_text_or_safe(*values: Any) -> str | None:
                text = _first_text(*values)
                if text:
                    return text
                for value in values:
                    text = _safe_text(value)
                    if text:
                        return text
                return None

            def _section_from_mapping(
                mapping: Mapping[str, Any],
                *,
                section_id: str,
                default_heading: str,
                fallback_excerpt: Any = None,
            ) -> dict[str, Any] | None:
                heading = (
                    _first_text_or_safe(
                        mapping.get("heading"),
                        mapping.get("title"),
                        mapping.get("label"),
                        mapping.get("event_type"),
                    )
                    or default_heading
                )
                excerpt = _first_text_or_safe(
                    mapping.get("excerpt"),
                    mapping.get("summary"),
                    mapping.get("abstract"),
                    mapping.get("text"),
                    mapping.get("content"),
                    mapping.get("body"),
                    mapping.get("snippet"),
                    mapping.get("description"),
                    mapping.get("message"),
                    fallback_excerpt,
                )
                if not excerpt:
                    details = mapping.get("details")
                    if isinstance(details, Mapping):
                        excerpt = _first_text_or_safe(
                            details.get("summary"),
                            details.get("description"),
                            details.get("comment"),
                            details.get("reason"),
                        )
                if not excerpt:
                    return None

                section: dict[str, Any] = {
                    "section_id": section_id,
                    "heading": heading,
                    "excerpt": excerpt,
                }
                citation = _first_text_or_safe(
                    mapping.get("citation"),
                    mapping.get("source_uri"),
                    mapping.get("source_url"),
                    mapping.get("url"),
                    mapping.get("href"),
                    mapping.get("pdf_url"),
                    mapping.get("doi"),
                )
                if citation:
                    section["citation"] = citation

                diff_summary = _first_text_or_safe(
                    mapping.get("diff_summary"),
                    mapping.get("change_summary"),
                )
                if diff_summary:
                    section["diff_summary"] = diff_summary

                task_links = _collect_renderer_task_links(mapping)
                if task_links:
                    section["task_links"] = task_links
                return section

            def _extract_sections(
                document_row: Mapping[str, Any],
                *,
                base_document_id: str,
            ) -> list[dict[str, Any]]:
                sections: list[dict[str, Any]] = []
                seen_section_ids: set[str] = set()

                def _append_section(
                    section: dict[str, Any] | None,
                    fallback_section_id: str,
                ) -> None:
                    if not isinstance(section, dict):
                        return
                    candidate = _first_text_or_safe(section.get("section_id"))
                    if not candidate:
                        candidate = fallback_section_id
                    section_id = candidate
                    suffix = 2
                    while section_id in seen_section_ids:
                        section_id = f"{candidate}_{suffix}"
                        suffix += 1
                    seen_section_ids.add(section_id)
                    section["section_id"] = section_id
                    sections.append(section)

                raw_sections = _mapping_list(document_row.get("sections"), limit=max_sections)
                for section_index, raw_section in enumerate(raw_sections, start=1):
                    raw_section_id = _first_text_or_safe(
                        raw_section.get("section_id"),
                        raw_section.get("id"),
                    ) or f"{base_document_id}_section_{section_index}"
                    section = _section_from_mapping(
                        raw_section,
                        section_id=raw_section_id,
                        default_heading=f"Section {section_index}",
                    )
                    _append_section(section, raw_section_id)
                    if len(sections) >= max_sections:
                        return sections

                collection_keys = (
                    "history",
                    "events",
                    "notes",
                    "chunks",
                    "matches",
                    "references",
                )
                for collection_key in collection_keys:
                    rows = _mapping_list(document_row.get(collection_key), limit=max_sections)
                    for row_index, row in enumerate(rows, start=1):
                        section_id = f"{base_document_id}_{collection_key}_{row_index}"
                        heading = (
                            _first_text_or_safe(
                                row.get("heading"),
                                row.get("title"),
                                row.get("event_type"),
                            )
                            or {
                                "history": f"History event {row_index}",
                                "events": f"Event {row_index}",
                                "notes": f"Note {row_index}",
                                "chunks": f"Chunk {row_index}",
                                "matches": f"Match {row_index}",
                                "references": f"Reference {row_index}",
                            }.get(collection_key, f"Section {row_index}")
                        )
                        section = _section_from_mapping(
                            row,
                            section_id=section_id,
                            default_heading=heading,
                        )
                        _append_section(section, section_id)
                        if len(sections) >= max_sections:
                            return sections

                if sections:
                    return sections

                fallback_section = _section_from_mapping(
                    document_row,
                    section_id=f"{base_document_id}_overview",
                    default_heading=(
                        _first_text_or_safe(document_row.get("heading")) or "Overview"
                    ),
                )
                _append_section(fallback_section, f"{base_document_id}_overview")
                return sections

            def _append_document(
                *,
                source_tool: str,
                document_row: Mapping[str, Any],
                payload_fallback: Mapping[str, Any] | None = None,
            ) -> None:
                if len(documents) >= max_documents:
                    return
                _register_source_tool(source_tool)

                payload_fallback = payload_fallback or {}
                base_document_id = (
                    _first_text_or_safe(
                        document_row.get("document_id"),
                        document_row.get("paper_id"),
                        document_row.get("arxiv_id"),
                        document_row.get("id"),
                        document_row.get("session_id"),
                        document_row.get("concept_id"),
                        payload_fallback.get("document_id"),
                        payload_fallback.get("arxiv_id"),
                    )
                    or f"{source_tool}_document_{len(documents) + 1}"
                )
                document_id = _next_document_id(base_document_id)

                title = (
                    _first_text_or_safe(
                        document_row.get("title"),
                        document_row.get("name"),
                        document_row.get("document_title"),
                        document_row.get("paper_title"),
                        payload_fallback.get("title"),
                        payload_fallback.get("name"),
                    )
                    or document_id
                )
                source_uri = _first_text_or_safe(
                    document_row.get("source_uri"),
                    document_row.get("uri"),
                    document_row.get("url"),
                    document_row.get("href"),
                    document_row.get("pdf_url"),
                    payload_fallback.get("source_uri"),
                    payload_fallback.get("url"),
                    payload_fallback.get("pdf_url"),
                )
                source_label = (
                    _first_text_or_safe(
                        document_row.get("source_label"),
                        document_row.get("source"),
                        document_row.get("source_name"),
                        payload_fallback.get("source_label"),
                        payload_fallback.get("source"),
                    )
                    or source_tool
                )
                updated_at = _first_text_or_safe(
                    document_row.get("updated_at"),
                    document_row.get("last_updated_at"),
                    document_row.get("published"),
                    document_row.get("created_at"),
                    payload_fallback.get("updated_at"),
                    payload_fallback.get("published"),
                )

                sections = _extract_sections(
                    document_row,
                    base_document_id=document_id,
                )
                if not sections:
                    return

                document_payload: dict[str, Any] = {
                    "document_id": document_id,
                    "title": title,
                    "source_label": source_label,
                    "sections": sections[:max_sections],
                }
                if source_uri:
                    document_payload["source_uri"] = source_uri
                if updated_at:
                    document_payload["updated_at"] = updated_at
                documents.append(document_payload)

            tool_payloads = list(_iter_renderer_tool_result_payloads(tool_messages))
            if not tool_payloads:
                return []

            for tool_name, payload in tool_payloads:
                if len(documents) >= max_documents:
                    break

                if tool_name == "get_paper_metadata":
                    _append_document(
                        source_tool=tool_name,
                        document_row=payload,
                        payload_fallback=payload,
                    )
                    continue

                document_collections = (
                    payload.get("documents"),
                    payload.get("papers"),
                    payload.get("results"),
                    payload.get("items"),
                )
                appended_from_collection = False
                for collection in document_collections:
                    rows = _mapping_list(collection, limit=max_documents * 4)
                    if not rows:
                        continue
                    for row in rows:
                        _append_document(
                            source_tool=tool_name,
                            document_row=row,
                            payload_fallback=payload,
                        )
                        if len(documents) >= max_documents:
                            break
                    appended_from_collection = True
                    if len(documents) >= max_documents:
                        break

                if appended_from_collection:
                    continue

                _append_document(
                    source_tool=tool_name,
                    document_row=payload,
                    payload_fallback=payload,
                )

            if not documents:
                return []

            return [
                {
                    "element_id": "screen_document_view",
                    "intent": "structured_document_view",
                    "payload": {"documents": documents[:max_documents]},
                    "constraints": {
                        "supports_section_links": True,
                        "supports_citation_links": True,
                        "supports_excerpt_expand": True,
                    },
                    "provenance": {
                        "source": "tool_result_document_view",
                        "source_tools": source_tools,
                        "record_family": "documents",
                    },
                }
            ]

        def _extract_renderer_screen_relation_graph_elements(
            *,
            tool_messages: Sequence[Mapping[str, Any]] = (),
            focus_concept_id: Any = None,
        ) -> list[dict[str, Any]]:
            """Derive relation graph payloads from concept/relation tool outputs."""

            tool_payloads = list(_iter_renderer_tool_result_payloads(tool_messages))
            if not tool_payloads:
                return []

            max_nodes = 120
            max_edges = 240

            source_tools: list[str] = []
            seen_source_tools: set[str] = set()

            nodes_by_id: dict[str, dict[str, Any]] = {}
            text_node_id_by_value: dict[str, str] = {}
            edges: list[dict[str, Any]] = []
            seen_edge_keys: set[tuple[str, str, str, str]] = set()
            node_cap_hit = False
            edge_cap_hit = False

            def _register_source_tool(tool_name: str) -> None:
                if tool_name in seen_source_tools:
                    return
                seen_source_tools.add(tool_name)
                source_tools.append(tool_name)

            def _is_concept_node_id(value: str) -> bool:
                return bool(_renderer_concept_id_pattern.match(value))

            def _coerce_text(value: Any) -> str | None:
                if isinstance(value, str):
                    cleaned = value.strip()
                    return cleaned or None
                if value is None:
                    return None
                cleaned = str(value).strip()
                return cleaned or None

            def _text_node_id(value: str) -> str:
                existing = text_node_id_by_value.get(value)
                if existing:
                    return existing
                slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
                slug = slug[:40] or "value"
                candidate = f"text:{slug}"
                suffix = 2
                while candidate in nodes_by_id:
                    candidate = f"text:{slug}_{suffix}"
                    suffix += 1
                text_node_id_by_value[value] = candidate
                return candidate

            def _merge_task_links(
                node_entry: dict[str, Any], task_links: Sequence[dict[str, str]]
            ) -> None:
                if not task_links:
                    return
                existing_links = node_entry.get("task_links")
                if not isinstance(existing_links, list):
                    node_entry["task_links"] = list(task_links)
                    return
                seen_targets: set[tuple[str, str]] = set()
                for link in existing_links:
                    if not isinstance(link, Mapping):
                        continue
                    seen_targets.add(
                        (str(link.get("link_type") or ""), str(link.get("target_id") or ""))
                    )
                for link in task_links:
                    key = (
                        str(link.get("link_type") or ""),
                        str(link.get("target_id") or ""),
                    )
                    if key in seen_targets:
                        continue
                    seen_targets.add(key)
                    existing_links.append(dict(link))

            def _ensure_node(
                raw_value: Any,
                *,
                label: Any = None,
                node_kind: Any = None,
                group: Any = None,
                task_link_values: Sequence[Any] = (),
            ) -> str | None:
                nonlocal node_cap_hit

                value_text = _coerce_text(raw_value)
                if not value_text:
                    return None

                if _is_concept_node_id(value_text):
                    node_id = value_text
                else:
                    node_id = _text_node_id(value_text)

                if node_id in nodes_by_id:
                    task_links = _collect_renderer_task_links(*task_link_values)
                    if task_links:
                        _merge_task_links(nodes_by_id[node_id], task_links)
                    return node_id

                if len(nodes_by_id) >= max_nodes:
                    node_cap_hit = True
                    return None

                label_text = _coerce_text(label) or value_text
                if len(label_text) > 120:
                    label_text = f"{label_text[:117]}..."

                kind_text = _coerce_text(node_kind)
                if not kind_text:
                    kind_text = "concept" if _is_concept_node_id(value_text) else "text"
                kind_text = kind_text.lower()

                node_entry: dict[str, Any] = {
                    "node_id": node_id,
                    "label": label_text,
                    "node_kind": kind_text,
                }
                group_text = _coerce_text(group)
                if group_text:
                    node_entry["group"] = group_text

                task_links = _collect_renderer_task_links(*task_link_values)
                if task_links:
                    node_entry["task_links"] = task_links

                nodes_by_id[node_id] = node_entry
                return node_id

            def _add_edge(
                source_id: str | None,
                target_id: str | None,
                predicate: Any,
                *,
                direction: Any = "directed",
                weight: Any = None,
            ) -> None:
                nonlocal edge_cap_hit

                if not source_id or not target_id:
                    return
                predicate_text = _coerce_text(predicate)
                if not predicate_text:
                    return

                direction_text = (_coerce_text(direction) or "directed").lower()
                if direction_text not in {"directed", "undirected"}:
                    direction_text = "directed"

                edge_key = (source_id, target_id, predicate_text, direction_text)
                if edge_key in seen_edge_keys:
                    return

                if len(edges) >= max_edges:
                    edge_cap_hit = True
                    return

                edge_entry: dict[str, Any] = {
                    "edge_id": f"edge_{len(edges) + 1}",
                    "source": source_id,
                    "target": target_id,
                    "predicate": predicate_text,
                    "direction": direction_text,
                }
                if isinstance(weight, (int, float)) and not isinstance(weight, bool):
                    edge_entry["weight"] = float(weight)

                seen_edge_keys.add(edge_key)
                edges.append(edge_entry)

            focus_node_text = _coerce_text(focus_concept_id)
            focus_node_id = _ensure_node(
                focus_node_text,
                node_kind="concept",
                group="focus",
            )

            for tool_name, tool_payload in tool_payloads:
                has_graph_data = False

                direct_nodes = _mapping_list(tool_payload.get("nodes"), limit=max_nodes * 3)
                direct_edges = _mapping_list(tool_payload.get("edges"), limit=max_edges * 3)
                if direct_nodes and direct_edges:
                    _register_source_tool(tool_name)
                    has_graph_data = True

                    for node in direct_nodes:
                        node_id = _first_text(
                            node.get("node_id"),
                            node.get("concept_id"),
                            node.get("id"),
                            node.get("label"),
                        )
                        _ensure_node(
                            node_id,
                            label=_first_text(node.get("label"), node.get("name"), node_id),
                            node_kind=_first_text(node.get("node_kind"), node.get("kind")),
                            group=_first_text(node.get("group")),
                            task_link_values=(node,),
                        )

                    for edge in direct_edges:
                        source_id = _ensure_node(
                            _first_text(
                                edge.get("source"),
                                edge.get("source_node_id"),
                                edge.get("from"),
                            ),
                            task_link_values=(edge,),
                        )
                        target_id = _ensure_node(
                            _first_text(
                                edge.get("target"),
                                edge.get("target_node_id"),
                                edge.get("to"),
                            ),
                            task_link_values=(edge,),
                        )
                        _add_edge(
                            source_id,
                            target_id,
                            _first_text(edge.get("predicate"), edge.get("label")),
                            direction=_first_text(edge.get("direction")),
                            weight=edge.get("weight"),
                        )

                if tool_name == "get_predicate_extent":
                    extent_rows = _mapping_list(tool_payload.get("extent"), limit=max_edges * 3)
                    if extent_rows:
                        _register_source_tool(tool_name)
                        has_graph_data = True

                    predicate_fallback = _first_text(
                        tool_payload.get("predicate_concept_id"),
                        tool_payload.get("concept_id"),
                    )
                    for row in extent_rows:
                        subject = _coerce_text(
                            _first_text(row.get("subject"), row.get("arg1"))
                        )
                        object_value = _coerce_text(row.get("object"))
                        if not object_value:
                            object_value = _coerce_text(row.get("arg2"))
                        predicate = _first_text(row.get("predicate"), predicate_fallback)
                        source_id = _ensure_node(
                            subject,
                            task_link_values=(row, tool_payload),
                        )
                        target_id = _ensure_node(
                            object_value,
                            task_link_values=(row, tool_payload),
                        )
                        _add_edge(source_id, target_id, predicate, direction="directed")

                concept_id = _first_text(tool_payload.get("concept_id"), tool_payload.get("id"))
                relationships = (
                    cast(Mapping[str, Any], tool_payload.get("relationships"))
                    if isinstance(tool_payload.get("relationships"), Mapping)
                    else {}
                )
                if concept_id and relationships:
                    _register_source_tool(tool_name)
                    has_graph_data = True
                    source_id = _ensure_node(
                        concept_id,
                        node_kind="concept",
                        group=(
                            "focus"
                            if focus_node_text and concept_id == focus_node_text
                            else None
                        ),
                        task_link_values=(tool_payload,),
                    )
                    for predicate_name, raw_targets in relationships.items():
                        predicate = _coerce_text(predicate_name)
                        if not predicate:
                            continue
                        if isinstance(raw_targets, (list, tuple)):
                            target_values = list(raw_targets)
                        else:
                            target_values = [raw_targets]
                        for raw_target in target_values:
                            if isinstance(raw_target, Mapping):
                                target_value = _first_text(
                                    raw_target.get("target"),
                                    raw_target.get("arg2"),
                                    raw_target.get("concept_id"),
                                    raw_target.get("id"),
                                    raw_target.get("value"),
                                )
                                target_label = _first_text(
                                    raw_target.get("label"),
                                    raw_target.get("name"),
                                )
                            else:
                                target_value = _coerce_text(raw_target)
                                target_label = None
                            target_id = _ensure_node(
                                target_value,
                                label=target_label,
                                task_link_values=(raw_target, tool_payload),
                            )
                            _add_edge(source_id, target_id, predicate, direction="directed")

                relation_list_keys = (
                    "relations",
                    "assertions",
                    "rows",
                    "results",
                )
                for relation_key in relation_list_keys:
                    relation_rows = _mapping_list(
                        tool_payload.get(relation_key),
                        limit=max_edges * 3,
                    )
                    if not relation_rows:
                        continue
                    extracted_any = False
                    for row in relation_rows:
                        predicate = _first_text(
                            row.get("predicate"),
                            row.get("predicate_id"),
                            row.get("relation"),
                        )
                        source_value = _first_text(
                            row.get("arg1"),
                            row.get("subject"),
                            row.get("source"),
                            row.get("from"),
                        )
                        target_value = _coerce_text(row.get("object"))
                        if not target_value:
                            target_value = _first_text(
                                row.get("arg2"),
                                row.get("target"),
                                row.get("to"),
                            )
                        if not (predicate and source_value and target_value):
                            continue
                        extracted_any = True
                        source_id = _ensure_node(
                            source_value,
                            task_link_values=(row, tool_payload),
                        )
                        target_id = _ensure_node(
                            target_value,
                            task_link_values=(row, tool_payload),
                        )
                        _add_edge(
                            source_id,
                            target_id,
                            predicate,
                            direction=_first_text(row.get("direction")),
                            weight=row.get("weight"),
                        )
                    if extracted_any:
                        _register_source_tool(tool_name)
                        has_graph_data = True
                        break

                if has_graph_data and (node_cap_hit or edge_cap_hit):
                    break

            if not nodes_by_id or not edges:
                return []

            if focus_node_text and _is_concept_node_id(focus_node_text):
                resolved_focus_node_id = focus_node_text
            else:
                resolved_focus_node_id = focus_node_id
            if resolved_focus_node_id and resolved_focus_node_id not in nodes_by_id:
                resolved_focus_node_id = None

            ordered_nodes = sorted(
                nodes_by_id.values(),
                key=lambda node: (
                    0
                    if resolved_focus_node_id
                    and str(node.get("node_id") or "") == resolved_focus_node_id
                    else 1,
                    str(node.get("node_id") or ""),
                ),
            )
            ordered_edges = sorted(
                edges,
                key=lambda edge: (
                    str(edge.get("source") or ""),
                    str(edge.get("target") or ""),
                    str(edge.get("predicate") or ""),
                    str(edge.get("direction") or ""),
                ),
            )
            for index, edge in enumerate(ordered_edges, start=1):
                edge["edge_id"] = f"edge_{index}"

            payload: dict[str, Any] = {
                "nodes": ordered_nodes,
                "edges": ordered_edges,
                "layout_hint": (
                    "radial_focus"
                    if resolved_focus_node_id
                    else "force_layers"
                ),
            }
            if resolved_focus_node_id:
                payload["focus_node_id"] = resolved_focus_node_id

            provenance: dict[str, Any] = {
                "source": "tool_result_relation_graph",
                "source_tools": source_tools,
                "record_family": "relation_graph",
                "node_count": len(ordered_nodes),
                "edge_count": len(ordered_edges),
            }
            if node_cap_hit or edge_cap_hit:
                provenance["truncated"] = True
                provenance["node_cap"] = max_nodes
                provenance["edge_cap"] = max_edges

            return [
                {
                    "element_id": "screen_relation_graph_view",
                    "intent": "relation_graph_view",
                    "payload": payload,
                    "constraints": {
                        "supports_pan_zoom": True,
                        "supports_clickthrough": True,
                    },
                    "provenance": provenance,
                }
            ]

        def _build_renderer_request_payload(
            screen_text: Any,
            *,
            tool_invocations: Sequence[Mapping[str, Any]],
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            screen_value = (
                screen_text.strip() if isinstance(screen_text, str) else str(screen_text)
            )
            context_tags: list[str] = ["chat_turn_rendering"]
            if presenter_mode_requested:
                context_tags.append("presenter_mode")
            if selector_verdict == "narration":
                context_tags.append("workflow:narration")
            elif selector_verdict in {"tool_seeking", "summarisation"}:
                context_tags.append("workflow:tool_calling")
            elif selector_verdict in {"plain_response", "fallback"}:
                context_tags.append("workflow:assistant")

            temporal_metadata: dict[str, Any] = {}
            if isinstance(turn_id, str) and turn_id.strip():
                temporal_metadata["turn_id"] = turn_id.strip()

            provenance: dict[str, Any] = {
                "source": "internal_mcp_orchestrator",
                "selected_workflow_id": selected_workflow_id,
                "presenter_mode_requested": bool(presenter_mode_requested),
            }
            if isinstance(conversation_session_id, str) and conversation_session_id.strip():
                provenance["conversation_session_id"] = conversation_session_id.strip()

            concept_candidates = _extract_renderer_concept_candidates(tool_invocations)
            predicate_hints = _extract_renderer_predicate_hints(tool_invocations)

            request_payload: dict[str, Any] = {
                "preferred_modalities": list(renderer_preferred_modalities),
                "context_tags": context_tags,
                "provenance": provenance,
            }
            if predicate_hints:
                request_payload["present_predicates"] = list(predicate_hints)

            selected_concept_id = concept_candidates[0] if concept_candidates else None
            if isinstance(selected_concept_id, str) and selected_concept_id:
                request_payload["object_kind"] = "concept"
                request_payload["concept_id"] = selected_concept_id
            else:
                # Preserve previous semantics: if no concept context is available,
                # treat the render object as a transient microtheory request.
                request_payload["object_kind"] = "transient_microtheory"
                request_payload["transient_microtheory"] = {
                    "context_scope": "chat_turn_rendering",
                    "assertions": (
                        [{"kind": "screen_text", "char_count": len(screen_value)}]
                        if screen_value
                        else []
                    ),
                    "theorems": [],
                    "provenance": provenance,
                    "episode_id": (
                        conversation_session_id.strip()
                        if isinstance(conversation_session_id, str)
                        and conversation_session_id.strip()
                        else None
                    ),
                    "temporal_metadata": temporal_metadata,
                }

            diagnostics = {
                "concept_candidates": list(concept_candidates),
                "selected_concept_id": selected_concept_id,
                "predicate_hint_count": len(predicate_hints),
                "request_object_kind": request_payload.get("object_kind"),
            }
            return request_payload, diagnostics

        _RENDERER_SCREEN_ELEMENT_FAMILY_MAP: dict[str, tuple[str, ...]] = {
            "table": ("table",),
            "tabular": ("table",),
            "calendar": ("calendar_view",),
            "calendar_view": ("calendar_view",),
            "citation": ("document_view",),
            "document": ("document_view",),
            "document_view": ("document_view",),
            "kanban": ("kanban_view",),
            "kanban_view": ("kanban_view",),
            "graph": ("relation_graph_view",),
            "relation_graph": ("relation_graph_view",),
            "relation_graph_view": ("relation_graph_view",),
            "task": ("task_view",),
            "task_view": ("task_view",),
            "timeline": ("timeline",),
            "workflow": ("workflow_view",),
            "workflow_view": ("workflow_view",),
        }
        _LEGACY_SCREEN_ELEMENT_FAMILIES: frozenset[str] = frozenset(
            {"table", "workflow_view"}
        )

        def _normalise_selected_renderer_types(raw_types: Sequence[Any]) -> list[str]:
            normalised: list[str] = []
            for raw_type in raw_types:
                if not isinstance(raw_type, str):
                    continue
                renderer_type = raw_type.strip().lower()
                if not renderer_type or renderer_type in normalised:
                    continue
                normalised.append(renderer_type)
            return normalised

        def _apply_screen_element_mapping(
            decision: dict[str, Any],
            *,
            tool_messages: Sequence[Mapping[str, Any]],
            resolver_attempted: bool,
            resolver_success: bool,
            selected_renderer_types: Sequence[str],
        ) -> None:
            renderer_types = _normalise_selected_renderer_types(selected_renderer_types)
            selected_families: set[str] = set()
            unsupported_renderer_types: list[str] = []
            reason_codes: list[str] = []

            if not resolver_attempted:
                mapping_mode = "fallback_not_attempted"
                selected_families.update(_LEGACY_SCREEN_ELEMENT_FAMILIES)
                reason_codes.append("renderer_screen_elements:fallback_not_attempted")
            elif not resolver_success:
                mapping_mode = "fallback_resolver_unsuccessful"
                selected_families.update(_LEGACY_SCREEN_ELEMENT_FAMILIES)
                reason_codes.append(
                    "renderer_screen_elements:fallback_resolver_unsuccessful"
                )
            elif not renderer_types:
                mapping_mode = "fallback_no_renderer_selection"
                selected_families.update(_LEGACY_SCREEN_ELEMENT_FAMILIES)
                reason_codes.append(
                    "renderer_screen_elements:fallback_no_renderer_selection"
                )
            else:
                mapping_mode = "selected_renderer_types"
                reason_codes.append("renderer_screen_elements:selected_renderer_types")
                for renderer_type in renderer_types:
                    mapped_families = _RENDERER_SCREEN_ELEMENT_FAMILY_MAP.get(
                        renderer_type
                    )
                    if not mapped_families:
                        unsupported_renderer_types.append(renderer_type)
                        continue
                    selected_families.update(mapped_families)
                if unsupported_renderer_types:
                    reason_codes.append(
                        "renderer_screen_elements:unsupported_renderer_types"
                    )
                if not selected_families:
                    reason_codes.append(
                        "renderer_screen_elements:no_structured_screen_family_selected"
                    )

            include_table_elements = "table" in selected_families
            include_workflow_elements = "workflow_view" in selected_families
            include_task_view_elements = "task_view" in selected_families
            include_calendar_elements = "calendar_view" in selected_families
            include_document_elements = "document_view" in selected_families
            include_kanban_elements = "kanban_view" in selected_families
            include_timeline_elements = "timeline" in selected_families
            include_relation_graph_elements = "relation_graph_view" in selected_families
            decision["screen_element_mapping_mode"] = mapping_mode
            decision["screen_element_reason_codes"] = list(reason_codes)
            decision["screen_element_families"] = sorted(selected_families)
            decision["screen_element_targets"] = {
                "table": include_table_elements,
                "workflow_view": include_workflow_elements,
                "task_view": include_task_view_elements,
                "calendar_view": include_calendar_elements,
                "document_view": include_document_elements,
                "kanban_view": include_kanban_elements,
                "timeline": include_timeline_elements,
                "relation_graph_view": include_relation_graph_elements,
            }
            if unsupported_renderer_types:
                decision["unsupported_selected_renderer_types"] = list(
                    unsupported_renderer_types
                )

            if include_table_elements:
                screen_table_record_sets = _extract_renderer_screen_table_record_sets(
                    tool_messages=tool_messages
                )
                if screen_table_record_sets:
                    decision["screen_table_record_sets"] = screen_table_record_sets
                    decision["screen_table_record_set_count"] = len(
                        screen_table_record_sets
                    )

            if include_workflow_elements:
                screen_workflow_elements = _extract_renderer_screen_workflow_elements(
                    tool_messages=tool_messages
                )
                if screen_workflow_elements:
                    decision["screen_workflow_elements"] = screen_workflow_elements
                    decision["screen_workflow_element_count"] = len(
                        screen_workflow_elements
                    )

            if include_task_view_elements:
                screen_task_view_elements = _extract_renderer_screen_task_view_elements(
                    tool_messages=tool_messages
                )
                if screen_task_view_elements:
                    decision["screen_task_view_elements"] = screen_task_view_elements
                    decision["screen_task_view_element_count"] = len(
                        screen_task_view_elements
                    )

            if include_calendar_elements:
                screen_calendar_elements = (
                    _extract_renderer_screen_calendar_elements(
                        tool_messages=tool_messages
                    )
                )
                if screen_calendar_elements:
                    decision["screen_calendar_elements"] = screen_calendar_elements
                    decision["screen_calendar_element_count"] = len(
                        screen_calendar_elements
                    )

            if include_document_elements:
                screen_document_elements = (
                    _extract_renderer_screen_document_elements(
                        tool_messages=tool_messages
                    )
                )
                if screen_document_elements:
                    decision["screen_document_elements"] = screen_document_elements
                    decision["screen_document_element_count"] = len(
                        screen_document_elements
                    )

            if include_kanban_elements:
                screen_kanban_elements = _extract_renderer_screen_kanban_elements(
                    tool_messages=tool_messages
                )
                if screen_kanban_elements:
                    decision["screen_kanban_elements"] = screen_kanban_elements
                    decision["screen_kanban_element_count"] = len(
                        screen_kanban_elements
                    )

            if include_timeline_elements:
                screen_timeline_elements = _extract_renderer_screen_timeline_elements(
                    tool_messages=tool_messages
                )
                if screen_timeline_elements:
                    decision["screen_timeline_elements"] = screen_timeline_elements
                    decision["screen_timeline_element_count"] = len(
                        screen_timeline_elements
                    )

            if include_relation_graph_elements:
                screen_relation_graph_elements = (
                    _extract_renderer_screen_relation_graph_elements(
                        tool_messages=tool_messages,
                        focus_concept_id=decision.get(
                            "request_payload_selected_concept_id"
                        ),
                    )
                )
                if screen_relation_graph_elements:
                    decision["screen_relation_graph_elements"] = (
                        screen_relation_graph_elements
                    )
                    decision["screen_relation_graph_element_count"] = len(
                        screen_relation_graph_elements
                    )

        def _resolve_renderer_render_plan(
            screen_text: Any,
            *,
            tool_invocations: Sequence[Mapping[str, Any]] = (),
            tool_messages: Sequence[Mapping[str, Any]] = (),
        ) -> Mapping[str, Any]:
            nonlocal renderer_render_plan
            if renderer_render_plan is not None:
                return dict(renderer_render_plan)

            decision: dict[str, Any] = {
                "type": "renderer_applicability_routing",
                "enabled": renderer_routing_enabled,
                "attempted": False,
                "success": False,
                "reason": "not_evaluated",
                "renderer_definition_count": len(renderer_definition_concept_ids),
                "renderer_definition_source": renderer_definition_source,
                "renderer_definition_ids": list(renderer_definition_concept_ids),
                "bootstrap_default_renderer_ids_enabled": (
                    bootstrap_default_renderer_ids_enabled
                ),
                "preferred_modalities": list(renderer_preferred_modalities),
                "allow_multimodal": renderer_allow_multimodal,
                "render_mode": "screen_only",
                "should_narrate": False,
                "selected_renderer_ids": [],
                "selected_renderer_types": [],
                "selected_modalities": [],
            }
            renderer_render_plan = decision

            if not renderer_routing_enabled:
                decision["reason"] = "feature_flag_disabled"
                _apply_screen_element_mapping(
                    decision,
                    tool_messages=tool_messages,
                    resolver_attempted=False,
                    resolver_success=False,
                    selected_renderer_types=(),
                )
                return dict(decision)

            if not renderer_definition_concept_ids:
                decision["reason"] = "renderer_definition_ids_missing"
                _apply_screen_element_mapping(
                    decision,
                    tool_messages=tool_messages,
                    resolver_attempted=False,
                    resolver_success=False,
                    selected_renderer_types=(),
                )
                aux_llm_calls.append(dict(decision))
                return dict(decision)

            request_payload, payload_diagnostics = _build_renderer_request_payload(
                screen_text,
                tool_invocations=tool_invocations,
            )
            decision["request_payload_object_kind"] = payload_diagnostics.get(
                "request_object_kind"
            )
            decision["request_payload_selected_concept_id"] = payload_diagnostics.get(
                "selected_concept_id"
            )
            decision["request_payload_concept_candidates"] = payload_diagnostics.get(
                "concept_candidates",
                [],
            )
            decision["request_payload_predicate_hint_count"] = payload_diagnostics.get(
                "predicate_hint_count",
                0,
            )

            payload: dict[str, Any] = {
                "renderer_definition_concept_ids": list(renderer_definition_concept_ids),
                "allow_multimodal": renderer_allow_multimodal,
                "request_payload": request_payload,
            }
            if isinstance(user_namespace, str) and user_namespace.strip():
                payload["namespace"] = user_namespace.strip()

            decision["attempted"] = True
            try:
                invocation_result = self._gateway.invoke(
                    "renderer_resolve_applicability",
                    payload,
                )
            except Exception as exc:
                decision["reason"] = "tool_error"
                decision["error"] = str(exc)
                _apply_screen_element_mapping(
                    decision,
                    tool_messages=tool_messages,
                    resolver_attempted=True,
                    resolver_success=False,
                    selected_renderer_types=(),
                )
                aux_llm_calls.append(dict(decision))
                return dict(decision)

            result_payload = (
                invocation_result.payload
                if hasattr(invocation_result, "payload")
                else (
                    invocation_result if isinstance(invocation_result, Mapping) else None
                )
            )
            if not isinstance(result_payload, Mapping):
                decision["reason"] = "invalid_tool_payload"
                _apply_screen_element_mapping(
                    decision,
                    tool_messages=tool_messages,
                    resolver_attempted=True,
                    resolver_success=False,
                    selected_renderer_types=(),
                )
                aux_llm_calls.append(dict(decision))
                return dict(decision)

            success = bool(result_payload.get("success"))
            decision["success"] = success
            if not success:
                decision["reason"] = "resolver_unsuccessful"
                error_text = result_payload.get("error")
                if isinstance(error_text, str) and error_text.strip():
                    decision["error"] = error_text.strip()
                error_code = result_payload.get("error_code")
                if isinstance(error_code, str) and error_code.strip():
                    decision["resolver_error_code"] = error_code.strip()
                error_details = result_payload.get("error_details")
                if isinstance(error_details, Mapping):
                    decision["resolver_error_details"] = dict(error_details)
                suggestions = result_payload.get("suggestions")
                if isinstance(suggestions, list):
                    decision["resolver_suggestions"] = [
                        str(item).strip()
                        for item in suggestions
                        if isinstance(item, str) and str(item).strip()
                    ]
                _apply_screen_element_mapping(
                    decision,
                    tool_messages=tool_messages,
                    resolver_attempted=True,
                    resolver_success=False,
                    selected_renderer_types=(),
                )
                aux_llm_calls.append(dict(decision))
                return dict(decision)

            selected_renderers_raw = result_payload.get("selected_renderers")
            selected_renderers = (
                selected_renderers_raw if isinstance(selected_renderers_raw, list) else []
            )
            selected_renderer_ids: list[str] = []
            selected_renderer_types: list[str] = []
            selected_modalities: list[str] = []
            narration_selected = False
            for item in selected_renderers:
                if not isinstance(item, Mapping):
                    continue
                renderer_id = str(item.get("renderer_id") or "").strip()
                if renderer_id and renderer_id not in selected_renderer_ids:
                    selected_renderer_ids.append(renderer_id)

                renderer_type_raw = str(item.get("renderer_type") or "").strip()
                renderer_type = renderer_type_raw.lower()
                if renderer_type and renderer_type not in selected_renderer_types:
                    selected_renderer_types.append(renderer_type)
                if renderer_type == "narration":
                    narration_selected = True

                modalities_raw = item.get("modalities")
                modalities_iter: list[str] = []
                if isinstance(modalities_raw, list):
                    modalities_iter = [str(modality) for modality in modalities_raw]
                elif isinstance(modalities_raw, tuple):
                    modalities_iter = [str(modality) for modality in modalities_raw]
                elif isinstance(modalities_raw, str):
                    modalities_iter = [modalities_raw]

                for modality in modalities_iter:
                    normalised = modality.strip().lower()
                    if not normalised:
                        continue
                    if normalised == "narrated_audio":
                        narration_selected = True
                    if normalised not in selected_modalities:
                        selected_modalities.append(normalised)

            diagnostics_obj = result_payload.get("diagnostics")
            if isinstance(diagnostics_obj, Mapping):
                selection_rationale = diagnostics_obj.get("selection_rationale")
                if isinstance(selection_rationale, str) and selection_rationale.strip():
                    decision["selection_rationale"] = selection_rationale.strip()
                loading_diag = diagnostics_obj.get("renderer_definition_loading")
                if isinstance(loading_diag, Mapping):
                    decision["renderer_definition_loading"] = dict(loading_diag)

            decision["reason"] = "resolved"
            decision["should_narrate"] = narration_selected
            decision["render_mode"] = (
                "spoken+screen" if narration_selected else "screen_only"
            )
            decision["selected_renderer_ids"] = selected_renderer_ids
            decision["selected_renderer_types"] = selected_renderer_types
            decision["selected_modalities"] = selected_modalities
            _apply_screen_element_mapping(
                decision,
                tool_messages=tool_messages,
                resolver_attempted=True,
                resolver_success=True,
                selected_renderer_types=selected_renderer_types,
            )
            aux_llm_calls.append(dict(decision))
            return dict(decision)

        def _maybe_apply_narration_routing(
            screen_text: Any,
            *,
            tool_invocations: Sequence[Mapping[str, Any]] = (),
            tool_messages: Sequence[Mapping[str, Any]] = (),
        ) -> Any:
            renderer_plan = _resolve_renderer_render_plan(
                screen_text,
                tool_invocations=tool_invocations,
                tool_messages=tool_messages,
            )
            renderer_mode = str(renderer_plan.get("render_mode") or "").strip().lower()
            should_route_narration = (
                selector_requests_narration or renderer_mode == "spoken+screen"
            )
            if not should_route_narration:
                return screen_text
            narration_workflow_id = _resolve_workflow_id_for_action_contract(
                required_action_ids=_NARRATION_ACTION_IDS,
                preferred_workflow_id=selected_workflow_id_text,
            )
            if narration_workflow_id is None:
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
                    "conversation_session_id": conversation_session_id,
                    "turn_id": turn_id,
                    "workflow_episode_source": "chat_turn_workflow",
                    "workflow_episode_stage": "narration",
                }

                narration_result = self.execute_workflow(
                    narration_workflow_id,
                    data=narration_data,
                    llm_client=llm_client,
                    model=_model_for_stage("narration"),
                    user_namespace=user_namespace,
                    auxiliary_system_prompt=auxiliary_system_prompt,
                    trace=None,
                    conversation_session_id=conversation_session_id,
                    turn_id=turn_id,
                    episode_source="chat_turn_workflow",
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
                                "workflow_id": narration_workflow_id,
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

        def _result_render_plan() -> Mapping[str, Any] | None:
            if not renderer_routing_enabled:
                return None
            if not isinstance(renderer_render_plan, dict):
                return None
            return dict(renderer_render_plan)

        # ----------------------------------------------------------------
        # JVNAUTOSCI-825: Route turns to the appropriate pathway.
        #
        # Three routing tiers:
        #   1. plain_response  — direct LLM call without tool context
        #   2. custom_workflow — execute selected discovered workflow
        #   3. tool_pipeline   — execute registry workflow matching tool contract
        # ----------------------------------------------------------------
        selected_uses_tool_pipeline_contract = _workflow_matches_action_contract(
            selected_workflow_id_text,
            required_action_ids=_TOOL_PIPELINE_ACTION_IDS,
        )

        # Tier 1: Plain response — skip tool-calling overhead entirely.
        # When the classifier says "plain_response", there is no need
        # to build tool context, inject write-policy, or run the
        # plan→validate→execute→backfill pipeline.  This saves an LLM
        # round-trip worth of system prompt tokens and reduces latency.
        if selector_verdict == "plain_response":
            planner_model = _model_for_stage("planner")
            if trace_enabled and trace is not None:
                llm_step = trace.start_step(
                    "llm.generate",
                    inputs={
                        "prompt": prompt,
                        "model": planner_model or "default",
                        "routing": "plain_response",
                    },
                )
            response, planner_model, _ = self._run_llm_with_fallbacks(
                stage="planner",
                prompt=prompt,
                context=augmented_context,
                default_client=llm_client,
                default_model=planner_model,
                policy_state=policy_state,
                registry_snapshot=registry_snapshot,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
                llm_calls_log=llm_calls,
                aux_log=aux_llm_calls,
                record_llm_call=_record_llm_call,
                emit_progress=_emit_progress_local,
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
            response_text = response if isinstance(response, str) else str(response)
            response_text = _maybe_apply_critic(response_text)
            response_text = _maybe_append_completion_claim_validation(response_text)
            result = OrchestratorResult(
                response_text=response_text,
                extra_messages=(),
                tool_invocations=(),
                aux_llm_calls=tuple(aux_llm_calls),
                llm_calls=tuple(llm_calls),
                llm_usage=_aggregate_usage_total(),
                orchestrator_duration_ms=_orchestrator_duration_ms(),
                workflow_routing=routing_info,
                render_plan=_result_render_plan(),
            )
            _persist_trace(status="completed")
            return result

        # Tier 2: Discovered workflow — dispatch directly through the
        # registry path when the selector explicitly chose a custom ID.
        if (
            selector_requests_custom_workflow
            and selected_workflow_id_text
            and not selected_uses_tool_pipeline_contract
        ):
            try:
                wf_result = self.execute_workflow(
                    selected_workflow_id_text,
                    data={
                        "prompt": prompt,
                        "augmented_context": augmented_context,
                        "user_concept_id": user_concept_id,
                        "org_concept_id": org_concept_id,
                        "gmail_profile": gmail_profile or self._default_gmail_profile,
                        "aux_llm_calls": aux_llm_calls,
                        "policy_state": policy_state,
                        "registry_snapshot": registry_snapshot,
                        "conversation_session_id": conversation_session_id,
                        "turn_id": turn_id,
                        "workflow_episode_source": "chat_turn_workflow",
                        "workflow_episode_stage": "workflow_dispatch",
                    },
                    llm_client=llm_client,
                    model=model,
                    user_namespace=user_namespace,
                    auxiliary_system_prompt=auxiliary_system_prompt,
                    trace=trace if trace_enabled else None,
                    conversation_session_id=conversation_session_id,
                    turn_id=turn_id,
                    episode_source="chat_turn_workflow",
                )
                if wf_result is not None:
                    wf_response = wf_result.data.get("response_text", "")
                    if not isinstance(wf_response, str) or not wf_response.strip():
                        wf_response = wf_result.data.get("summary", "")
                    if not isinstance(wf_response, str) or not wf_response.strip():
                        wf_response = (
                            f"Workflow {selected_workflow_id_text} completed "
                            f"(state: {wf_result.final_state})."
                        )
                    wf_response = _maybe_append_completion_claim_validation(wf_response)
                    aux_llm_calls.append(
                        {
                            "type": "workflow_execution",
                            "workflow_id": selected_workflow_id_text,
                            "final_state": wf_result.final_state,
                            "completed": wf_result.completed,
                        }
                    )
                    result = OrchestratorResult(
                        response_text=wf_response,
                        extra_messages=(),
                        tool_invocations=(),
                        aux_llm_calls=tuple(aux_llm_calls),
                        llm_calls=tuple(llm_calls),
                        llm_usage=_aggregate_usage_total(),
                        orchestrator_duration_ms=_orchestrator_duration_ms(),
                        workflow_routing=routing_info,
                    )
                    _persist_trace(status="completed")
                    return result
                self._logger.warning(
                    "[mcp_orchestrator] Selected workflow %s not in registry; "
                    "falling through to tool-calling.",
                    selected_workflow_id_text,
                )
            except Exception as exc:
                self._logger.warning(
                    "[mcp_orchestrator] Selected workflow %s failed: %s; "
                    "falling through to tool-calling.",
                    selected_workflow_id_text,
                    exc,
                )

        # ----------------------------------------------------------------
        # JVNAUTOSCI-922 Phase 2: Route tool calling through the workflow
        # engine via a registry-discovered tool pipeline workflow.
        # ----------------------------------------------------------------
        tool_dispatch_workflow_id = _resolve_workflow_id_for_action_contract(
            required_action_ids=_TOOL_PIPELINE_ACTION_IDS,
            preferred_workflow_id=selected_workflow_id_text,
        )
        if tool_dispatch_workflow_id is None:
            self._logger.error(
                "[mcp_orchestrator] no registry workflow satisfies the "
                "tool-calling action contract"
            )
            response_text = _maybe_apply_narration_routing(
                "I attempted to use tools but no executable tool-calling workflow "
                "is available. Please try again or report this issue.",
                tool_invocations=(),
                tool_messages=(),
            )
            result = OrchestratorResult(
                response_text=response_text,
                extra_messages=(),
                tool_invocations=(),
                aux_llm_calls=tuple(aux_llm_calls),
                llm_calls=tuple(llm_calls),
                llm_usage=_aggregate_usage_total(),
                orchestrator_duration_ms=_orchestrator_duration_ms(),
                workflow_routing=routing_info,
                render_plan=_result_render_plan(),
            )
            _persist_trace(status="completed")
            return result

        tc_env = WorkflowEnvironment(
            llm_client=llm_client,
            gateway=self._gateway,
            model=model,
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
            max_tool_invocations=self._max_tool_invocations,
            default_gmail_profile=gmail_profile or self._default_gmail_profile,
        )
        auto_proceed_minimal_imposition_enabled = (
            self._get_auto_proceed_minimal_imposition_enabled()
        )
        try:
            aux_llm_calls.append(
                {
                    "type": "auto_proceed_minimal_imposition_setting",
                    "enabled": bool(auto_proceed_minimal_imposition_enabled),
                    "source": "settings",
                }
            )
        except Exception:
            pass

        routing_info_payload = asdict(routing_info) if routing_info is not None else None
        tc_data: dict[str, Any] = {
            # Inputs.
            "prompt": prompt,
            "augmented_context": augmented_context,
            "policy_state": policy_state,
            "registry_snapshot": registry_snapshot,
            "user_concept_id": user_concept_id,
            "org_concept_id": org_concept_id,
            "conversation_session_id": conversation_session_id,
            "turn_id": turn_id,
            "recent_user_prompts": recent_user_prompts,
            "gmail_profile": gmail_profile or self._default_gmail_profile,
            "workflow_discovery_result": workflow_discovery_result,
            "workflow_routing": routing_info_payload,
            "auto_proceed_minimal_imposition_enabled": bool(
                auto_proceed_minimal_imposition_enabled
            ),
            "workflow_episode_source": "chat_turn_workflow",
            "workflow_episode_stage": "tool_calling",
            # Closures from run().
            "model_for_stage": _model_for_stage,
            "record_llm_call": _record_llm_call,
            "emit_progress": _emit_progress_local,
            "emit_phase_transition": _emit_phase_transition_local,
            "check_cancellation": _check_cancellation_local,
            "build_parse_error_result": _build_tool_call_parse_error_result,
            "build_validation_error_result": _build_tool_call_validation_error_result,
            # Shared mutable state.
            "aux_llm_calls": aux_llm_calls,
            "llm_calls": llm_calls,
            "invocations": [],
            "tool_messages": [],
            # Inter-handler state initialised here; handlers override.
            "iteration_count": 0,
            "allowed_write_tools": set(),
            # Shared missing-tool-call retry budget for the full turn across
            # both planning and backfill recovery passes.
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": int(
                self._max_missing_tool_call_retries_per_turn
            ),
        }

        tc_result = self.execute_workflow(
            tool_dispatch_workflow_id,
            data=tc_data,
            llm_client=llm_client,
            model=model,
            user_namespace=user_namespace,
            auxiliary_system_prompt=auxiliary_system_prompt,
            trace=trace if trace_enabled else None,
            environment=tc_env,
            conversation_session_id=conversation_session_id,
            turn_id=turn_id,
            episode_source="chat_turn_workflow",
        )
        if tc_result is None:
            result = OrchestratorResult(
                response_text=(
                    "I attempted to use tools but the selected tool-calling "
                    "workflow is not available. Please try again or report this issue."
                ),
                extra_messages=(),
                tool_invocations=(),
                aux_llm_calls=tuple(aux_llm_calls),
                llm_calls=tuple(llm_calls),
                llm_usage=_aggregate_usage_total(),
                orchestrator_duration_ms=_orchestrator_duration_ms(),
                workflow_routing=routing_info,
            )
            _persist_trace(status="completed")
            return result

        # Unpack the workflow result.
        if tc_result.data.get("orchestrator_result") is not None:
            # Handler produced a pre-built OrchestratorResult (error case).
            _persist_trace(status="completed")
            return tc_result.data["orchestrator_result"]

        final_response = tc_result.data.get("final_response", "")
        if not isinstance(final_response, str):
            final_response = str(final_response)
        tool_messages = tc_result.data.get("tool_messages", [])
        invocations = tc_result.data.get("invocations", [])
        iteration_count = tc_result.data.get("iteration_count", 0)

        final_response_text = _maybe_apply_narration_routing(
            final_response,
            tool_invocations=(
                tuple(invocations) if isinstance(invocations, (list, tuple)) else ()
            ),
            tool_messages=(
                tuple(tool_messages)
                if isinstance(tool_messages, (list, tuple))
                else ()
            ),
        )
        final_response_text = _maybe_apply_critic(
            final_response_text, tool_messages_for_critic=tool_messages
        )
        final_response_text = _maybe_append_completion_claim_validation(
            final_response_text,
            tool_invocations_for_validation=tuple(invocations),
            tool_messages_for_validation=tuple(tool_messages),
        )

        gate_requires_follow_up = bool(
            tc_result.data.get("completion_gate_requires_follow_up", False)
        )
        gate_safe_to_claim_completion = bool(
            tc_result.data.get(
                "completion_gate_safe_to_claim_completion",
                not gate_requires_follow_up,
            )
        )
        terminal_trace_status = (
            "completed"
            if gate_safe_to_claim_completion and not gate_requires_follow_up
            else "follow_up_required"
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
            workflow_routing=routing_info,
            render_plan=_result_render_plan(),
        )
        _persist_trace(status=terminal_trace_status)
        return result


__all__ = [
    "InternalMCPChatOrchestrator",
    "OrchestratorResult",
    "ToolCallParsingError",
    "WorkflowRoutingInfo",
]
