"""Reusable durable workflow action: tool-result signal extraction.

JVNAUTOSCI-2118 (Phase 1 vertical slice of Epic JVNAUTOSCI-2112). This action
exposes the existing Python primitive
``tool_result_hints.extract_signals_from_tool_result`` as a generic VWL action
so workflow definitions can chain "fetch via tool X" -> "extract structured
signals from X's output" without any tool-specific Python.

The action is fully generic: the prompt body that drives extraction is read
from a Vontology text relation authored against the source tool concept (by
default ``#V#output_item_signal_extraction_hint``). Callers supply only:

* ``source_tool_concept`` -- the tool concept id whose authored hint to use
* ``tool_payload``        -- the raw output of that tool
* (optional) ``hint_predicate_id`` -- override the hint predicate
* (optional) ``lang``     -- language for the hint resolution

Anti-drift: this module names no specific external integration. All semantic
behaviour lives in Vontology hint bodies.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ...services.output_hint_contracts import (
    OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
)
from ...services.tool_result_hints import extract_signals_from_tool_result
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)


logger = logging.getLogger(__name__)


EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID = "extract_signals_from_tool_result"
"""Canonical action id used by VWL workflow definitions."""


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _resolve_tool_payload(inputs: Mapping[str, Any], data: Mapping[str, Any]) -> Any:
    """Pick up the tool payload from inputs first, then fall back to shared data.

    VWL ``hasInputMap`` will normally bind ``tool_payload`` directly via an
    output-field-to-context-key edge, but allowing a ``data`` fallback keeps
    the action robust to authoring patterns that stash the previous step's
    output under a known context key.
    """

    if "tool_payload" in inputs:
        return inputs["tool_payload"]
    return data.get("tool_payload")


def _handle_extract_signals_from_tool_result(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    data = request.data if isinstance(request.data, dict) else {}

    source_tool_concept = (
        _safe_str(inputs.get("source_tool_concept"))
        or _safe_str(inputs.get("source_tool_concept_id"))
        or _safe_str(inputs.get("tool_concept_id"))
    )
    if not source_tool_concept:
        return WorkflowActionResult(
            status="failed",
            error=(
                "extract_signals_from_tool_result requires 'source_tool_concept' "
                "(the Vontology concept id of the tool whose authored signal-"
                "extraction hint should drive extraction)."
            ),
        )

    tool_payload = _resolve_tool_payload(inputs, data)
    if tool_payload is None:
        return WorkflowActionResult(
            status="failed",
            error=(
                "extract_signals_from_tool_result requires 'tool_payload' "
                "(the raw output of the source tool)."
            ),
        )

    hint_predicate_id = (
        _safe_str(inputs.get("hint_predicate_id"))
        or OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID
    )
    lang = _safe_str(inputs.get("lang")) or "en-NZ"
    model = _safe_str(inputs.get("model")) or None

    llm_client = request.environment.llm_client
    if llm_client is None:
        return WorkflowActionResult(
            status="failed",
            error=(
                "extract_signals_from_tool_result requires an LLM client on the "
                "workflow environment."
            ),
        )

    result = extract_signals_from_tool_result(
        source_tool_concept,
        tool_payload,
        turn_context=data,
        llm_client=llm_client,
        model=model,
        hint_predicate_id=hint_predicate_id,
        lang=lang,
    )

    outputs: dict[str, Any] = {
        "signals": dict(result.signals) if isinstance(result.signals, Mapping) else result.signals,
        "hint_resolved": bool(result.hint_resolved),
        "hint_body_present": bool(result.hint_body),
        "warnings": list(result.warnings),
        "raw_response": result.raw_response,
        "source_tool_concept": source_tool_concept,
        "hint_predicate_id": hint_predicate_id,
    }

    # Treat "hint not authored" or any LLM-side failure as a workflow failure
    # so on_failure transitions can react. Empty signals with no warnings is a
    # legitimate "nothing to extract" success.
    failure_warnings = {"hint_not_authored", "no_llm_client", "empty_response"}
    if any(
        w in failure_warnings or w.startswith("llm_error:")
        for w in result.warnings
    ):
        return WorkflowActionResult(
            status="failed",
            outputs=outputs,
            error=", ".join(result.warnings) or "extract_signals_from_tool_result failed",
        )

    return WorkflowActionResult(status="success", outputs=outputs)


def register_tool_result_hint_actions(registry: ActionRegistry) -> None:
    """Register the extract_signals_from_tool_result action.

    Idempotent: safe to call multiple times against the same registry.
    """

    registry.register_if_absent(
        ActionSpec(
            action_id=EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID,
            handler=_handle_extract_signals_from_tool_result,
            description=(
                "Generic durable action: read the Vontology-authored signal-"
                "extraction hint for the named source tool concept, run the "
                "shared LLM-backed extractor over the supplied tool payload, "
                "and surface the structured signals plus diagnostic warnings."
            ),
        )
    )


__all__ = [
    "EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID",
    "register_tool_result_hint_actions",
]
