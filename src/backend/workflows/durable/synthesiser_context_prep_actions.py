"""Reusable durable workflow action: synthesiser context preparation.

JVNAUTOSCI-2117 introduced the late-turn support step that preserves the active
request and Vontology-authored tool-output hints for the synthesiser. The
message wording itself is now resolved from a represented prompt/template
concept; this module only extracts inputs, resolves hint bodies, binds template
variables, and stages the resulting records for the summariser.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ...services.output_hint_contracts import (
    OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID,
    OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID,
)
from ...services.synthesiser_context_framing_service import (
    SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID,
    SYNTHESISER_CONTEXT_FRAMING_PROMPT_INPUT_KEY,
    SynthesiserContextFramingTemplateError,
    render_active_request_framing,
    render_tool_hints_framing,
    resolve_synthesiser_context_framing_template,
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
    for key in (
        "user_message_text",
        "prompt",
        "prompt_for_requirements",
        "active_user_message",
        "current_user_message",
        "turn_prompt",
    ):
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


def _resolve_tool_hint_payload(
    tool_concept_id: str,
    *,
    lang: str = "en-NZ",
) -> dict[str, Any] | None:
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
    return {
        "tool_concept_id": tool_concept_id,
        "collection_presentation_hint": presentation,
        "item_summary_hint": item_summary,
        "hint_predicate_ids": [
            predicate_id
            for predicate_id, hint_body in (
                (OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID, presentation),
                (OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID, item_summary),
            )
            if hint_body
        ],
    }


def _message_content(message: Any) -> str:
    if isinstance(message, Mapping):
        return _safe_str(message.get("content"))
    return _safe_str(message)


def _message_source_summary(message: Any) -> dict[str, Any]:
    if not isinstance(message, Mapping):
        return {}
    source_keys = (
        "source",
        "requested_prompt_concept_id",
        "source_prompt_concept_id",
        "template_schema",
        "template_field",
        "tool_concept_id",
        "hint_predicate_ids",
    )
    return {
        key: message.get(key)
        for key in source_keys
        if message.get(key) not in (None, "", [], {})
    }


def _resolve_framing_prompt_concept_id(
    request: WorkflowActionRequest,
    inputs: Mapping[str, Any],
) -> str:
    explicit = _safe_str(inputs.get(SYNTHESISER_CONTEXT_FRAMING_PROMPT_INPUT_KEY))
    if explicit:
        return explicit

    for container in (request.prompt_contract, request.workflow_state_metadata):
        if not isinstance(container, Mapping):
            continue
        prompt_contract = container
        if "prompt_contract" in container and isinstance(
            container.get("prompt_contract"),
            Mapping,
        ):
            prompt_contract = container["prompt_contract"]  # type: ignore[index]
        resolved = _safe_str(prompt_contract.get("resolved_prompt_concept_id"))
        if resolved:
            return resolved
        requested_prompt_ids = prompt_contract.get("requested_prompt_concept_ids")
        if isinstance(requested_prompt_ids, Sequence) and not isinstance(
            requested_prompt_ids,
            (str, bytes, bytearray),
        ):
            for prompt_id in requested_prompt_ids:
                candidate = _safe_str(prompt_id)
                if candidate:
                    return candidate

    return SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID


def _handle_synthesiser_context_prep(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    """Resolve hints for tools that ran this turn and stage system messages.

    The handler does not directly mutate ``augmented_context``: that injection
    is the responsibility of the orchestrator stage that consumes
    ``synthesiser_system_messages``. Staging via ``data`` keeps this action pure
    and easy to test.
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

    hint_payloads: list[dict[str, Any]] = []
    for concept_id in tool_concept_ids:
        payload = _resolve_tool_hint_payload(concept_id, lang=lang)
        if payload:
            hint_payloads.append(payload)

    if not user_message and not hint_payloads:
        final_messages: list[Any] = []
        if isinstance(request.data, dict):
            existing = request.data.get(_SYNTHESISER_PREP_MESSAGES_KEY)
            if isinstance(existing, list):
                final_messages = list(existing)
            else:
                request.data[_SYNTHESISER_PREP_MESSAGES_KEY] = []
        return WorkflowActionResult(
            status="success",
            outputs={
                _SYNTHESISER_PREP_MESSAGES_KEY: list(final_messages),
                "system_messages": [],
                "system_message_records": [],
                "synthesiser_system_message_sources": [],
                "tool_concept_ids_seen": list(tool_concept_ids),
                "hints_resolved_count": 0,
                "active_user_message_present": False,
                "context_framing_template_required": False,
            },
        )

    prompt_concept_id = _resolve_framing_prompt_concept_id(request, inputs)
    template, template_diagnostics = resolve_synthesiser_context_framing_template(
        prompt_concept_id=prompt_concept_id,
    )
    if template is None:
        return WorkflowActionResult(
            status="failed",
            error="synthesiser_context_framing_template_unavailable",
            outputs={
                "result": False,
                "tool_concept_ids_seen": list(tool_concept_ids),
                "hints_resolved_count": len(hint_payloads),
                "active_user_message_present": bool(user_message),
                "context_framing_template_required": True,
                "context_framing_template_diagnostics": dict(template_diagnostics),
            },
        )

    system_message_records: list[dict[str, Any]] = []
    try:
        active_request = render_active_request_framing(
            template,
            active_user_message=user_message,
        )
        if active_request:
            system_message_records.append(active_request)

        for payload in hint_payloads:
            message = render_tool_hints_framing(
                template,
                tool_concept_id=str(payload.get("tool_concept_id") or ""),
                collection_presentation_hint=str(
                    payload.get("collection_presentation_hint") or ""
                ),
                item_summary_hint=str(payload.get("item_summary_hint") or ""),
                hint_predicate_ids=payload.get("hint_predicate_ids") or (),
            )
            if message:
                system_message_records.append(message)
    except SynthesiserContextFramingTemplateError as exc:
        return WorkflowActionResult(
            status="failed",
            error=str(exc),
            outputs={
                "result": False,
                "tool_concept_ids_seen": list(tool_concept_ids),
                "hints_resolved_count": len(hint_payloads),
                "active_user_message_present": bool(user_message),
                "context_framing_template_required": True,
                "context_framing_template_diagnostics": dict(template_diagnostics),
            },
        )

    final_messages: list[Any] = list(system_message_records)

    # Persist back onto the shared turn data so downstream stages can consume
    # it without redoing the resolution. Append rather than replace so multiple
    # invocations within a turn accumulate cleanly (each call only adds new
    # messages produced by the current snapshot of invocations).
    if isinstance(request.data, dict):
        existing = request.data.get(_SYNTHESISER_PREP_MESSAGES_KEY)
        if isinstance(existing, list):
            existing_contents = {_message_content(message) for message in existing}
            for message in system_message_records:
                content = _message_content(message)
                if not content or content in existing_contents:
                    continue
                existing.append(message)
                existing_contents.add(content)
            final_messages = list(existing)
        else:
            request.data[_SYNTHESISER_PREP_MESSAGES_KEY] = list(system_message_records)
            final_messages = list(system_message_records)

    return WorkflowActionResult(
        status="success",
        outputs={
            _SYNTHESISER_PREP_MESSAGES_KEY: list(final_messages),
            "system_messages": [
                _message_content(message) for message in system_message_records
            ],
            "system_message_records": list(system_message_records),
            "synthesiser_system_message_sources": [
                _message_source_summary(message) for message in system_message_records
            ],
            "tool_concept_ids_seen": list(tool_concept_ids),
            "hints_resolved_count": len(hint_payloads),
            "active_user_message_present": bool(user_message),
            "context_framing_template_required": True,
            "context_framing_template_diagnostics": dict(template_diagnostics),
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
