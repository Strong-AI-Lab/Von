"""Canonical turn-level display element contract for chat responses.

JVNAUTOSCI-1149 introduces a first-class response-construction model for
display elements so rendering decisions are explicit and inspectable.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
import re

DISPLAY_ELEMENT_SCHEMA_VERSION = "turn_display_elements_v1"

# Keep this allow-list explicit so element admission is deterministic and
# renderer integration can evolve without ad hoc shape drift.
ALLOWED_DISPLAY_ELEMENT_TYPES = frozenset(
    {
        "text_block",
        "json_block",
        "table",
        "timeline",
        "task_view",
        "workflow_view",
    }
)

_JSON_FENCE_PATTERN = re.compile(
    r"```json\s*\n(?P<body>[\s\S]*?)\n```",
    flags=re.IGNORECASE,
)


def _normalise_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _dedupe_preserve_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def extract_json_fences(text: str | None) -> list[str]:
    """Extract canonical fenced JSON blocks from text."""
    if not isinstance(text, str) or not text.strip():
        return []

    fences: list[str] = []
    for match in _JSON_FENCE_PATTERN.finditer(text):
        body = str(match.group("body") or "").strip()
        if not body:
            continue
        fences.append(f"```json\n{body}\n```")
    return _dedupe_preserve_order(fences)


def _extract_json_body_from_fence(fence: str) -> str:
    match = _JSON_FENCE_PATTERN.search(fence)
    if not match:
        return ""
    return str(match.group("body") or "").strip()


def validate_turn_display_elements(
    contract: Mapping[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Validate a turn-level display element contract deterministically."""
    errors: list[str] = []

    if not isinstance(contract, Mapping):
        return False, ["contract must be a mapping"]

    schema_version = contract.get("schema_version")
    if schema_version != DISPLAY_ELEMENT_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {DISPLAY_ELEMENT_SCHEMA_VERSION!r}, got {schema_version!r}"
        )

    elements = contract.get("elements")
    if not isinstance(elements, list):
        return False, [*errors, "elements must be a list"]

    for index, element in enumerate(elements):
        label = f"elements[{index}]"
        if not isinstance(element, Mapping):
            errors.append(f"{label} must be a mapping")
            continue

        element_id = element.get("element_id")
        if not isinstance(element_id, str) or not element_id.strip():
            errors.append(f"{label}.element_id must be a non-empty string")

        element_type = element.get("element_type")
        if element_type not in ALLOWED_DISPLAY_ELEMENT_TYPES:
            errors.append(
                f"{label}.element_type {element_type!r} not in allow-list {sorted(ALLOWED_DISPLAY_ELEMENT_TYPES)}"
            )

        order = element.get("order")
        if not isinstance(order, int):
            errors.append(f"{label}.order must be an integer")

        intent = element.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            errors.append(f"{label}.intent must be a non-empty string")

        payload = element.get("payload")
        if not isinstance(payload, Mapping):
            errors.append(f"{label}.payload must be a mapping")
            continue

        provenance = element.get("provenance")
        if not isinstance(provenance, Mapping):
            errors.append(f"{label}.provenance must be a mapping")

        if element_type == "text_block":
            text_value = payload.get("text")
            if not isinstance(text_value, str) or not text_value.strip():
                errors.append(f"{label}.payload.text must be a non-empty string")
        elif element_type == "json_block":
            fence = payload.get("fence")
            if not isinstance(fence, str) or not fence.strip():
                errors.append(f"{label}.payload.fence must be a non-empty string")
            elif "```json" not in fence.lower():
                errors.append(f"{label}.payload.fence must be a fenced JSON block")

    return len(errors) == 0, errors


def build_turn_display_elements(
    *,
    response_text: str | None,
    presenter_channels: Mapping[str, Any] | None,
    required_screen_json_fence: str | None = None,
    screen_backfill_second_pass_attempted: bool = False,
    screen_backfill_second_pass_reason: str | None = None,
    spoken_backfill_second_pass_attempted: bool = False,
    spoken_backfill_second_pass_reason: str | None = None,
) -> dict[str, Any]:
    """Build canonical display elements for a single response turn."""
    presenter_format = (
        presenter_channels.get("format")
        if isinstance(presenter_channels.get("format"), str)
        else None
    ) if isinstance(presenter_channels, Mapping) else None

    screen_text = (
        _normalise_text(presenter_channels.get("screen"))
        if isinstance(presenter_channels, Mapping)
        else None
    )
    spoken_text = (
        _normalise_text(presenter_channels.get("spoken"))
        if isinstance(presenter_channels, Mapping)
        else None
    )
    fallback_response_text = _normalise_text(response_text)

    reason_codes: list[str] = []
    if screen_backfill_second_pass_attempted:
        reason = _normalise_text(screen_backfill_second_pass_reason) or "unspecified"
        reason_codes.append(f"screen_backfill:{reason}")
    if spoken_backfill_second_pass_attempted:
        reason = _normalise_text(spoken_backfill_second_pass_reason) or "unspecified"
        reason_codes.append(f"spoken_backfill:{reason}")

    elements: list[dict[str, Any]] = []

    effective_screen = screen_text or fallback_response_text
    if effective_screen:
        screen_source = (
            "screen_backfill"
            if screen_backfill_second_pass_attempted
            else ("presenter_channel" if screen_text else "response_text")
        )
        elements.append(
            {
                "element_id": "screen_text",
                "element_type": "text_block",
                "channel": "screen",
                "order": 10,
                "intent": "primary_response",
                "payload": {"text": effective_screen},
                "constraints": {"preserve_fences": True},
                "provenance": {
                    "source": screen_source,
                    "presenter_format": presenter_format,
                    "reason_code": _normalise_text(screen_backfill_second_pass_reason),
                },
            }
        )

    if spoken_text:
        spoken_source = (
            "spoken_backfill"
            if spoken_backfill_second_pass_attempted
            else "presenter_channel"
        )
        elements.append(
            {
                "element_id": "spoken_text",
                "element_type": "text_block",
                "channel": "spoken",
                "order": 20,
                "intent": "narration",
                "payload": {"text": spoken_text},
                "constraints": {"tts_ready": True},
                "provenance": {
                    "source": spoken_source,
                    "presenter_format": presenter_format,
                    "reason_code": _normalise_text(spoken_backfill_second_pass_reason),
                },
            }
        )

    json_fences = extract_json_fences(effective_screen)
    required_fence = _normalise_text(required_screen_json_fence)
    if required_fence and required_fence not in json_fences:
        json_fences.append(required_fence)
        reason_codes.append("required_screen_json_fence_appended")

    for index, fence in enumerate(json_fences, start=1):
        elements.append(
            {
                "element_id": f"screen_json_block_{index}",
                "element_type": "json_block",
                "channel": "screen",
                "order": 10 + index,
                "intent": "verbatim_json_payload",
                "payload": {
                    "fence": fence,
                    "body": _extract_json_body_from_fence(fence),
                },
                "constraints": {"must_preserve_verbatim": True},
                "provenance": {
                    "source": (
                        "required_prompt_fence"
                        if required_fence and fence == required_fence
                        else "screen_text_fence"
                    ),
                    "required_by_user_prompt": bool(required_fence and fence == required_fence),
                },
            }
        )

    # Emit elements in the same deterministic order signalled by `order`.
    # This keeps downstream renderers and regressions aligned on one sequence.
    elements.sort(key=lambda item: (int(item.get("order", 0)), str(item.get("element_id", ""))))

    reason_codes = _dedupe_preserve_order(reason_codes)

    contract: dict[str, Any] = {
        "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
        "elements": elements,
        "reason_codes": reason_codes,
    }
    valid, errors = validate_turn_display_elements(contract)
    contract["validation"] = {
        "valid": valid,
        "errors": errors,
    }
    return contract
