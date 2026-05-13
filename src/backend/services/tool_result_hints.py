"""Generic Vontology-authored signal extraction from tool result payloads.

JVNAUTOSCI-2117 (Phase 0 of Epic JVNAUTOSCI-2112) introduces a tool-agnostic
primitive that resolves an ``output_item_signal_extraction_hint`` text relation
from a tool concept in Vontology and uses it -- and only it -- to instruct an
LLM to extract structured signals from the tool's output payload.

Key design principle (anti-drift gate): this module contains **no references to
any specific external service or domain**. All tool-specific knowledge lives in
the hint body authored against the tool concept in Vontology. The Python here
is pure support surface (per AGENTS.md sections 3 and 14: prompts and policy
live in Vontology; Python is mechanism, not policy).

The companion bootstrap script ``scripts/bootstrap_output_hint_predicates.py``
authors the predicate concept; later vertical-slice tasks (JVNAUTOSCI-2118 and
onward) author hint bodies against individual tool concepts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from .output_hint_contracts import (
    OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
)
from .text_value_service import get_texts_for_concept


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMGenerationResult:
    """Provider-agnostic LLM result plus optional model-policy telemetry."""

    response: Any
    selected_model: str | None = None
    selected_candidate: Mapping[str, Any] | None = None
    llm_calls: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    aux_llm_calls: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


class LLMGenerationError(RuntimeError):
    """LLM generation failed, but the caller has telemetry worth preserving."""

    def __init__(
        self,
        message: str,
        *,
        error_class: str,
        llm_calls: tuple[Mapping[str, Any], ...] = (),
        aux_llm_calls: tuple[Mapping[str, Any], ...] = (),
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.llm_calls = llm_calls
        self.aux_llm_calls = aux_llm_calls


@dataclass(frozen=True)
class StructuredSignals:
    """Result of an extraction call.

    Attributes:
        signals: Parsed signal payload returned by the LLM after applying the
            hint body. Empty dict when no hint was found, the LLM returned no
            usable JSON, or extraction was skipped.
        hint_body: The resolved hint text used to drive the LLM call. Empty
            string when no hint was found.
        hint_resolved: True if a hint text relation was found on the tool
            concept; False if the tool concept has no signal-extraction hint
            authored yet (callers should treat this as a no-op rather than an
            error -- not every tool requires signals to be extracted).
        raw_response: The raw LLM response string (for telemetry / diagnosis).
        warnings: Non-fatal issues encountered during extraction (e.g. JSON
            parse failure, empty payload).
        llm_calls: Provider call telemetry captured by the caller, when the
            extraction used the workflow model-policy runner.
        aux_llm_calls: Model-policy and fallback-chain telemetry captured by
            the caller, when available.
    """

    signals: Mapping[str, Any]
    hint_body: str
    hint_resolved: bool
    raw_response: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)
    llm_calls: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    aux_llm_calls: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    selected_model: str | None = None
    selected_candidate: Mapping[str, Any] | None = None


def resolve_hint_body(
    tool_concept_id: str,
    hint_predicate_id: str,
    *,
    lang: str = "en-NZ",
) -> str:
    """Fetch the first hint body text relation matching predicate, or "".

    Returns the empty string when the tool concept has no such hint authored.
    Callers must tolerate this -- a missing hint is a Vontology-authoring gap,
    not a runtime error.
    """

    if not tool_concept_id or not hint_predicate_id:
        return ""

    try:
        rows = get_texts_for_concept(
            subject_concept_id=tool_concept_id,
            predicate=hint_predicate_id,
            lang=lang,
            limit=1,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "resolve_hint_body failed for %s/%s: %s",
            tool_concept_id,
            hint_predicate_id,
            exc,
        )
        return ""

    if not rows:
        return ""

    text = rows[0].get("text")
    if isinstance(text, str):
        return text.strip()
    return ""


def _parse_signals(raw_response: str) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    """Best-effort JSON parse of the LLM response into a mapping.

    Tolerates fenced code blocks (```json ... ```) since hint authors are
    encouraged to ask for JSON output but cannot prevent every model from
    wrapping it. Returns (parsed_mapping, warnings).
    """

    if not raw_response or not raw_response.strip():
        return {}, ("empty_response",)

    candidate = raw_response.strip()

    # Strip a leading code fence if present.
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        if first_newline != -1:
            candidate = candidate[first_newline + 1 :]
        if candidate.endswith("```"):
            candidate = candidate[: -3]
        candidate = candidate.strip()

    # Find the first top-level JSON object/array if there is preamble text.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end != -1 and end > start:
            slice_ = candidate[start : end + 1]
            try:
                parsed = json.loads(slice_)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, Mapping):
                return parsed, ()
            if isinstance(parsed, list):
                return {"items": parsed}, ()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {}, ("json_parse_failed",)

    if isinstance(parsed, Mapping):
        return parsed, ()
    if isinstance(parsed, list):
        return {"items": parsed}, ()
    return {}, ("non_object_response",)


def _serialise_payload(payload: Any, *, max_chars: int = 12_000) -> str:
    """JSON-serialise the tool payload with a defensive size cap."""

    try:
        serialised = json.dumps(payload, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        serialised = str(payload)
    if len(serialised) > max_chars:
        return serialised[:max_chars] + "...[truncated]"
    return serialised


def _normalise_generation_result(
    generation_result: Any,
) -> tuple[
    str,
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    str | None,
    Mapping[str, Any] | None,
]:
    if isinstance(generation_result, LLMGenerationResult):
        response = generation_result.response
        selected_candidate = (
            dict(generation_result.selected_candidate)
            if isinstance(generation_result.selected_candidate, Mapping)
            else None
        )
        return (
            response if isinstance(response, str) else str(response),
            tuple(generation_result.llm_calls),
            tuple(generation_result.aux_llm_calls),
            generation_result.selected_model,
            selected_candidate,
        )
    return (
        (
            generation_result
            if isinstance(generation_result, str)
            else str(generation_result)
        ),
        (),
        (),
        None,
        None,
    )


def extract_signals_from_tool_result(
    source_tool_concept_id: str,
    tool_payload: Any,
    turn_context: Optional[Mapping[str, Any]] = None,
    *,
    llm_client: Any,
    model: Optional[str] = None,
    llm_generate: Callable[..., Any] | None = None,
    hint_predicate_id: str = OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
    lang: str = "en-NZ",
) -> StructuredSignals:
    """Extract structured signals from a tool result via a Vontology-authored hint.

    The flow is intentionally generic:

    1. Resolve the ``output_item_signal_extraction_hint`` text relation on the
       given tool concept. If absent, return an empty result with
       ``hint_resolved=False``.
    2. Build the LLM prompt **purely** from the resolved hint body plus the
       JSON-serialised tool payload. The hint body carries every tool-specific
       instruction.
    3. Call ``llm_client.generate(prompt, context, model)`` -- the canonical
       provider-agnostic surface (``LLMInterface.generate``).
    4. Parse the response best-effort as JSON and return a ``StructuredSignals``.

    Args:
        source_tool_concept_id: Vontology concept id of the tool whose payload
            should be processed.
        tool_payload: The raw tool result payload (any JSON-serialisable shape).
        turn_context: Optional shared turn context. Present for forward
            compatibility (e.g. so future hint authors can request inclusion of
            the active user message); not yet consumed by this generic path.
        llm_client: A ``LLMInterface``-shaped object exposing ``generate``.
            Injected so the primitive remains testable and respects the model
            portfolio at call sites.
        model: Optional model override forwarded to ``generate``.
        llm_generate: Optional provider-agnostic generation hook. Callers that
            need workflow model-policy fallbacks can provide this without
            changing the Vontology-authored hint body or embedding policy here.
        hint_predicate_id: The predicate to resolve. Defaults to the canonical
            signal-extraction hint; overridable for testing or for alternative
            hint families with the same shape.
        lang: Text-relation language preference.

    Returns:
        ``StructuredSignals``. Always returns a value -- this primitive does not
        raise on missing hints, model errors, or parse failures; it surfaces
        them via ``warnings`` so workflow callers can decide how to react.
    """

    # Forward-compatibility hook: turn_context is reserved for future authored
    # behaviour. Reference it so static analysers do not flag it as unused, but
    # do not assume any particular shape.
    _ = turn_context

    hint_body = resolve_hint_body(
        source_tool_concept_id, hint_predicate_id, lang=lang
    )
    if not hint_body:
        return StructuredSignals(
            signals={},
            hint_body="",
            hint_resolved=False,
            raw_response="",
            warnings=("hint_not_authored",),
        )

    if llm_client is None and llm_generate is None:
        return StructuredSignals(
            signals={},
            hint_body=hint_body,
            hint_resolved=True,
            raw_response="",
            warnings=("no_llm_client",),
        )

    payload_text = _serialise_payload(tool_payload)
    prompt = (
        f"{hint_body}\n\n"
        "Apply the instructions above to the following tool result payload "
        "and return a single JSON object containing the extracted signals.\n\n"
        f"Tool result payload (JSON):\n{payload_text}"
    )

    try:
        if llm_generate is not None:
            generation_result = llm_generate(
                prompt=prompt,
                context=None,
                model=model,
            )
        else:
            generation_result = llm_client.generate(
                prompt=prompt,
                context=None,
                model=model,
            )
    except Exception as exc:
        error_class = (
            exc.error_class
            if isinstance(exc, LLMGenerationError)
            else type(exc).__name__
        )
        llm_calls = exc.llm_calls if isinstance(exc, LLMGenerationError) else ()
        aux_llm_calls = (
            exc.aux_llm_calls if isinstance(exc, LLMGenerationError) else ()
        )
        logger.warning(
            "extract_signals_from_tool_result LLM call failed for %s: %s",
            source_tool_concept_id,
            exc,
        )
        return StructuredSignals(
            signals={},
            hint_body=hint_body,
            hint_resolved=True,
            raw_response="",
            warnings=(f"llm_error:{error_class}",),
            llm_calls=tuple(llm_calls),
            aux_llm_calls=tuple(aux_llm_calls),
        )

    (
        response_text,
        llm_calls,
        aux_llm_calls,
        selected_model,
        selected_candidate,
    ) = _normalise_generation_result(generation_result)
    signals, warnings = _parse_signals(response_text)
    return StructuredSignals(
        signals=signals,
        hint_body=hint_body,
        hint_resolved=True,
        raw_response=response_text,
        warnings=warnings,
        llm_calls=llm_calls,
        aux_llm_calls=aux_llm_calls,
        selected_model=selected_model,
        selected_candidate=selected_candidate,
    )


__all__ = [
    "LLMGenerationError",
    "LLMGenerationResult",
    "StructuredSignals",
    "extract_signals_from_tool_result",
    "resolve_hint_body",
]
