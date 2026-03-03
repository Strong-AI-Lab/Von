"""Shared helpers for buttonify output transformation.

Keep heuristics and JSON option parsing centralised so route-level and
workflow-level callers stay behaviourally aligned.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

BUTTONIFY_PROMPT_IDS: tuple[str, ...] = ("#V#buttonify_prompt_v1",)
_BUTTONIFY_PROMPT_CONTRACT_SUFFIX = (
    "\n\nMandatory quality gate:\n"
    "- Options MUST be actionable next user messages (imperative actions or direct confirmations).\n"
    "- Do NOT output noun/topic fragments.\n"
    '- If uncertain, output [].'
)
_BUTTONIFY_FILTERING_BOUNDARY_SCHEMA_VERSION = "buttonify_filtering_boundary_v1"
_BUTTONIFY_FILTERING_PREVIEW_LIMIT = 8


def _normalise_buttonify_option_with_reason(value: str | None) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, "non_string"
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return None, "empty"
    cleaned = cleaned.strip("-–—•*\t ")
    cleaned = re.sub(r"^[\"'“‘]+|[\"'”’]+$", "", cleaned).strip()
    cleaned = cleaned.rstrip(".,;:")
    if not cleaned:
        return None, "empty"
    if len(cleaned) > 60:
        return None, "too_long_chars"
    if len(cleaned.split()) > 4:
        return None, "too_many_words"
    return cleaned, None


def normalise_buttonify_option(value: str | None) -> str | None:
    cleaned, _ = _normalise_buttonify_option_with_reason(value)
    return cleaned


def dedupe_buttonify_options(
    values: Iterable[str | None],
    *,
    max_options: int = 4,
) -> list[str]:
    options: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = normalise_buttonify_option(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        options.append(cleaned)
        if len(options) >= max_options:
            break
    return options


def extract_buttonify_options_heuristic(
    text: str | None,
    *,
    max_options: int = 4,
) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return []

    options: list[str] = []
    seen: set[str] = set()

    def _add_option(raw: str | None) -> None:
        nonlocal options
        if len(options) >= max_options:
            return
        cleaned = normalise_buttonify_option(raw)
        if not cleaned:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        options.append(cleaned)

    quote_patterns = [
        r'"([^"\n\r]{1,200})"',
        r"“([^”\n\r]{1,200})”",
        r"‘([^’\n\r]{1,200})’",
        r"(?<!\w)'([^'\n\r]{1,200})'(?!\w)",
    ]

    for pattern in quote_patterns:
        for match in re.findall(pattern, text):
            _add_option(match)
            if len(options) >= max_options:
                return options[:max_options]

    for line in text.splitlines():
        match = re.match(r"\s*(?:[-*•]|\d+[.)])\s+(.+)", line)
        if not match:
            continue
        _add_option(match.group(1))
        if len(options) >= max_options:
            return options[:max_options]

    if not options:
        marker = re.search(
            r"(?:reply|respond|answer|choose|pick)\s+(?:with\s+)?one\s+of\s*[:\-–—]?\s*(.+)",
            text,
            re.IGNORECASE,
        )
        if marker:
            tail = marker.group(1)
            for part in re.split(r"\s*(?:,|/|;|\bor\b)\s*", tail):
                _add_option(part)
                if len(options) >= max_options:
                    return options[:max_options]

    implicit = re.search(
        r"\b(?:would|do)\s+(?:you\s+)?(?:like|want)\s+(?:to\s+)?([^?.!\n]{1,80}?)\s+or\s+([^?.!\n]{1,80}?)[?.!]",
        text,
        re.IGNORECASE,
    )
    if implicit:
        _add_option(implicit.group(1))
        _add_option(implicit.group(2))

    if not options:
        if re.search(r"\byes\s*/\s*no\b|\byes\s+or\s+no\b", text, re.IGNORECASE):
            _add_option("Yes")
            _add_option("No")
        else:
            trimmed = text.strip()
            if trimmed.endswith("?") and re.match(
                r"\s*(?:Do|Would|Is|Are|Did|Can|Should|Will|Have|Has)\b",
                trimmed,
                re.IGNORECASE,
            ):
                _add_option("Yes")
                _add_option("No")

    return options[:max_options]


def parse_buttonify_options_json(
    raw_response: Any,
    *,
    max_options: int = 4,
) -> list[str]:
    options, _telemetry = parse_buttonify_options_json_with_telemetry(
        raw_response,
        max_options=max_options,
    )
    return options


def _option_preview(value: Any, *, max_chars: int = 80) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def parse_buttonify_options_json_with_telemetry(
    raw_response: Any,
    *,
    max_options: int = 4,
) -> tuple[list[str], dict[str, Any]]:
    telemetry: dict[str, Any] = {
        "schema_version": _BUTTONIFY_FILTERING_BOUNDARY_SCHEMA_VERSION,
        "source": "json",
        "parse_success": False,
        "parse_reason": None,
        "raw_response_kind": type(raw_response).__name__,
        "raw_response_preview": _option_preview(raw_response),
        "input_candidate_count": 0,
        "accepted_candidate_count": 0,
        "rejected_candidate_count": 0,
        "rejection_reason_counts": {},
        "accepted_preview": [],
        "rejected_preview": [],
    }
    try:
        parsed = json.loads(str(raw_response))
    except Exception as exc:
        telemetry["parse_reason"] = f"json_decode_failed:{type(exc).__name__}"
        return [], telemetry
    if not isinstance(parsed, list):
        telemetry["parse_reason"] = "json_payload_not_list"
        return [], telemetry

    telemetry["parse_success"] = True
    telemetry["parse_reason"] = "ok"
    telemetry["input_candidate_count"] = len(parsed)
    options, filtering_boundary = sanitise_buttonify_options_with_telemetry(
        (item for item in parsed if isinstance(item, str)),
        max_options=max_options,
    )
    telemetry.update(dict(filtering_boundary))
    telemetry["source"] = "json"
    telemetry["parse_success"] = True
    telemetry["parse_reason"] = "ok"
    return options, telemetry


_BUTTONIFY_CONCEPT_ID_PATTERN = re.compile(
    r"^#V#[A-Za-z0-9_@.\-]+$",
    re.IGNORECASE,
)
_BUTTONIFY_JIRA_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,24}-\d+$")
_BUTTONIFY_CODE_PUNCTUATION_PATTERN = re.compile(r"[`_{}[\]<>]")


def _looks_like_code_or_identifier(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if _BUTTONIFY_CONCEPT_ID_PATTERN.fullmatch(text):
        return True
    if _BUTTONIFY_JIRA_KEY_PATTERN.fullmatch(text):
        return True
    if _BUTTONIFY_CODE_PUNCTUATION_PATTERN.search(text):
        return True
    if "\\" in text or "/" in text:
        return True
    return False


def sanitise_buttonify_options(
    values: Iterable[str | None],
    *,
    max_options: int = 4,
) -> list[str]:
    """Remove code-like options so quick replies stay user-facing."""
    options, _telemetry = sanitise_buttonify_options_with_telemetry(
        values,
        max_options=max_options,
    )
    return options


def sanitise_buttonify_options_with_telemetry(
    values: Iterable[str | None],
    *,
    max_options: int = 4,
) -> tuple[list[str], dict[str, Any]]:
    """Return cleaned options plus deterministic filtering-boundary telemetry."""

    raw_values = list(values)
    accepted_candidates: list[str] = []
    accepted_seen: set[str] = set()
    rejection_reason_counts: dict[str, int] = {}
    rejected_preview: list[dict[str, Any]] = []

    def _reject(reason: str, value: Any) -> None:
        rejection_reason_counts[reason] = rejection_reason_counts.get(reason, 0) + 1
        if len(rejected_preview) >= _BUTTONIFY_FILTERING_PREVIEW_LIMIT:
            return
        rejected_preview.append(
            {
                "reason": reason,
                "value_preview": _option_preview(value),
            }
        )

    for raw_value in raw_values:
        cleaned, normalise_reason = _normalise_buttonify_option_with_reason(raw_value)
        if normalise_reason:
            _reject(normalise_reason, raw_value)
            continue
        if cleaned is None:
            _reject("empty", raw_value)
            continue
        cleaned_key = cleaned.lower()
        if cleaned_key in accepted_seen:
            _reject("duplicate", raw_value)
            continue
        if _looks_like_code_or_identifier(cleaned):
            _reject("code_or_identifier", raw_value)
            continue
        accepted_seen.add(cleaned_key)
        accepted_candidates.append(cleaned)

    overflow_count = max(0, len(accepted_candidates) - max_options)
    if overflow_count > 0:
        rejection_reason_counts["max_options_cap"] = (
            rejection_reason_counts.get("max_options_cap", 0) + overflow_count
        )

    accepted = accepted_candidates[:max_options]
    telemetry = {
        "schema_version": _BUTTONIFY_FILTERING_BOUNDARY_SCHEMA_VERSION,
        "source": "candidate_filter",
        "input_candidate_count": len(raw_values),
        "accepted_candidate_count": len(accepted),
        "rejected_candidate_count": int(sum(rejection_reason_counts.values())),
        "rejection_reason_counts": dict(
            sorted(rejection_reason_counts.items(), key=lambda item: item[0])
        ),
        "accepted_preview": list(accepted[:_BUTTONIFY_FILTERING_PREVIEW_LIMIT]),
        "rejected_preview": list(rejected_preview),
    }
    return accepted, telemetry


def sanitise_buttonify_heuristic_options(
    values: Iterable[str | None],
    *,
    max_options: int = 4,
) -> list[str]:
    """Compatibility wrapper for heuristic call sites."""
    return sanitise_buttonify_options(values, max_options=max_options)


def select_buttonify_preflight_options(
    values: Iterable[str | None],
    *,
    max_options: int = 4,
) -> tuple[list[str], str | None]:
    """Return preflight options only when confidence is high enough to skip LLM."""
    accepted = sanitise_buttonify_heuristic_options(values, max_options=max_options)
    if len(accepted) >= 2:
        return accepted, None
    if not accepted:
        return [], "heuristic_preflight_rejected_code_like_candidates"
    return [], "heuristic_preflight_low_confidence"


def enforce_buttonify_prompt_contract(prompt_text: str | None) -> str:
    """Append hard constraints so prompt concepts and fallback text share quality rules."""
    base = str(prompt_text or "").strip()
    suffix = _BUTTONIFY_PROMPT_CONTRACT_SUFFIX.strip()
    if not base:
        return suffix
    if suffix in base:
        return base
    return f"{base}\n\n{suffix}"


