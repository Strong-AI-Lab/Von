"""Reusable durable workflow action: synthesiser context preparation.

JVNAUTOSCI-2117 (Phase 0 of Epic JVNAUTOSCI-2112). This action is the
Vontology-authored replacement for the heuristic context compaction in
``orchestrator._build_follow_up_llm_context`` (the ``keep_recent_user_messages``
path). It runs late in the turn, just before the synthesiser LLM call, and
injects a system message into the shared turn context that:

* names the active user request, so the synthesiser stage cannot lose it to
  context trimming, and
* surfaces collection-presentation and per-item summary hints authored against
  each tool concept that produced results in this turn.

This task only registers the action. Phase 4 cutover -- replacing the existing
heuristic call site in the orchestrator -- is tracked separately as
JVNAUTOSCI-2110.

Anti-drift: this module references no specific external service or domain. All
tool-specific behaviour comes from hint bodies authored in Vontology against
individual tool concepts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ...services.output_hint_contracts import (
    OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID,
    OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID,
)
from ...services.tool_result_hints import resolve_hint_body
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)


logger = logging.getLogger(__name__)


SYNTHESISER_CONTEXT_PREP_ACTION_ID = "synthesiser_context_prep"
"""Canonical action id used by VWL workflow definitions."""


_SYNTHESISER_PREP_MESSAGES_KEY = "synthesiser_system_messages"
"""Key under which prepared system messages are stored on the shared turn data."""


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _extract_active_user_message(data: Mapping[str, Any]) -> str:
    for key in ("user_message_text", "prompt", "active_user_message"):
        text = _safe_str(data.get(key))
        if text:
            return text
    recent = data.get("recent_user_prompts")
    if isinstance(recent, Sequence) and not isinstance(recent, (str, bytes)):
        for entry in reversed(recent):
            text = _safe_str(entry)
            if text:
                return text
    return ""


def _extract_tool_concept_ids(invocations: Any) -> tuple[str, ...]:
    """Extract a stable, de-duplicated list of tool concept ids from invocations.

    Tolerates several common shapes used across the orchestrator -- each
    invocation may carry a ``tool_concept_id`` (preferred) or fall back to the
    raw ``tool`` name. Order is preserved by first appearance so synthesiser
    framing matches the order tools actually ran.
    """

    if not isinstance(invocations, Iterable) or isinstance(invocations, (str, bytes)):
        return ()

    ordered: list[str] = []
    seen: set[str] = set()
    for inv in invocations:
        if not isinstance(inv, Mapping):
            continue
        candidate = (
            _safe_str(inv.get("tool_concept_id"))
            or _safe_str(inv.get("concept_id"))
            or _safe_str(inv.get("tool"))
            or _safe_str(inv.get("name"))
        )
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return tuple(ordered)


def _build_active_request_message(user_message: str) -> str:
    if not user_message:
        return ""
    return f"Active request for this turn: {user_message}"


def _build_tool_hints_message(
    tool_concept_id: str, *, lang: str = "en-NZ"
) -> str | None:
    """Compose a system message bundling whichever hints are authored.

    Returns None if neither hint is present, so callers can drop the entry
    rather than emit empty system messages.
    """

    presentation = resolve_hint_body(
        tool_concept_id,
        OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID,
        lang=lang,
    )
    item_summary = resolve_hint_body(
        tool_concept_id,
        OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID,
        lang=lang,
    )
    if not presentation and not item_summary:
        return None

    parts: list[str] = [f"Synthesis hints for tool {tool_concept_id}:"]
    if presentation:
        parts.append(f"Collection presentation hint: {presentation}")
    if item_summary:
        parts.append(f"Per-item summary hint: {item_summary}")
    return "\n".join(parts)


def _handle_synthesiser_context_prep(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    """Resolve hints for tools that ran this turn and stage system messages.

    The handler does not directly mutate ``augmented_context``: that injection
    is the responsibility of the orchestrator stage that consumes
    ``synthesiser_system_messages`` (the Phase 4 cutover handled by
    JVNAUTOSCI-2110). Staging via ``data`` keeps this action pure and easy to
    test.
    """

    inputs = dict(request.inputs or {})
    data = request.data if isinstance(request.data, dict) else {}
    lang = _safe_str(inputs.get("lang")) or "en-NZ"

    # Honour explicit overrides from VWL inputs but fall back to shared data.
    user_message = (
        _safe_str(inputs.get("user_message_text"))
        or _extract_active_user_message(data)
    )
    invocations = inputs.get("invocations") or data.get("invocations") or ()
    tool_concept_ids = _extract_tool_concept_ids(invocations)

    system_messages: list[str] = []

    active_request = _build_active_request_message(user_message)
    if active_request:
        system_messages.append(active_request)

    hints_resolved = 0
    for concept_id in tool_concept_ids:
        message = _build_tool_hints_message(concept_id, lang=lang)
        if message:
            system_messages.append(message)
            hints_resolved += 1

    # Persist back onto the shared turn data so downstream stages can consume
    # it without redoing the resolution. Append rather than replace so multiple
    # invocations within a turn accumulate cleanly (each call only adds new
    # messages produced by the current snapshot of invocations).
    if isinstance(request.data, dict):
        existing = request.data.get(_SYNTHESISER_PREP_MESSAGES_KEY)
        if isinstance(existing, list):
            existing.extend(m for m in system_messages if m not in existing)
        else:
            request.data[_SYNTHESISER_PREP_MESSAGES_KEY] = list(system_messages)

    return WorkflowActionResult(
        status="success",
        outputs={
            "system_messages": list(system_messages),
            "tool_concept_ids_seen": list(tool_concept_ids),
            "hints_resolved_count": hints_resolved,
            "active_user_message_present": bool(user_message),
        },
    )


def register_synthesiser_context_prep_actions(registry: ActionRegistry) -> None:
    """Register the synthesiser_context_prep action.

    Idempotent: safe to call multiple times against the same registry.
    """

    registry.register_if_absent(
        ActionSpec(
            action_id=SYNTHESISER_CONTEXT_PREP_ACTION_ID,
            handler=_handle_synthesiser_context_prep,
            description=(
                "Resolve Vontology-authored output_collection_presentation_hint "
                "and output_item_summary_hint text relations for each tool "
                "concept that ran this turn, plus the active user message, and "
                "stage them under data['synthesiser_system_messages'] for the "
                "synthesiser stage to inject."
            ),
        )
    )


__all__ = [
    "SYNTHESISER_CONTEXT_PREP_ACTION_ID",
    "register_synthesiser_context_prep_actions",
]
