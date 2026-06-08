from __future__ import annotations

from typing import Any, Mapping


TURN_OUTPUT_HEALTH_SCHEMA_VERSION = "turn_output_health_v1"


def _dedupe_turn_output_health_issues(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any, Any, Any]] = set()
    unique: list[dict[str, Any]] = []
    for issue in issues:
        key = (
            issue.get("category"),
            issue.get("code"),
            issue.get("severity"),
            issue.get("message"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def build_turn_output_health(debug_info: Mapping[str, Any] | None) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if not isinstance(debug_info, Mapping):
        return {
            "schema_version": TURN_OUTPUT_HEALTH_SCHEMA_VERSION,
            "status": "ok",
            "issues": [],
        }

    presenter_channels = debug_info.get("presenter_channels")
    if isinstance(presenter_channels, Mapping):
        screen_value = presenter_channels.get("screen")
        spoken_value = presenter_channels.get("spoken")
        screen_ok = isinstance(screen_value, str) and bool(screen_value.strip())
        spoken_ok = isinstance(spoken_value, str) and bool(spoken_value.strip())

        if screen_ok and not spoken_ok:
            issues.append(
                {
                    "category": "presenter_output",
                    "code": "missing_spoken_channel",
                    "severity": "warning",
                    "message": "Presenter output missing spoken channel; text-to-speech will fall back to screen text.",
                    "fallback_used": "screen_text_for_tts",
                }
            )
        elif spoken_ok and not screen_ok:
            issues.append(
                {
                    "category": "presenter_output",
                    "code": "missing_screen_channel",
                    "severity": "warning",
                    "message": "Presenter output missing screen channel; display will fall back to spoken text.",
                    "fallback_used": "spoken_text_for_display",
                }
            )
        elif not screen_ok and not spoken_ok:
            issues.append(
                {
                    "category": "presenter_output",
                    "code": "empty_presenter_channels",
                    "severity": "error",
                    "message": "Presenter output present but both screen and spoken channels are empty.",
                }
            )

    spoken_backfill_attempted = bool(
        debug_info.get("spoken_backfill_second_pass_attempted")
    )
    if spoken_backfill_attempted:
        spoken_still_missing = True
        if isinstance(presenter_channels, Mapping):
            spoken_value = presenter_channels.get("spoken")
            spoken_still_missing = not (
                isinstance(spoken_value, str) and bool(spoken_value.strip())
            )
        if spoken_still_missing:
            spoken_backfill_reason = debug_info.get("spoken_backfill_second_pass_reason")
            reason_text = (
                str(spoken_backfill_reason).strip()
                if isinstance(spoken_backfill_reason, str)
                and spoken_backfill_reason.strip()
                else "unknown_reason"
            )
            issues.append(
                {
                    "category": "presenter_output",
                    "code": "spoken_backfill_missing_spoken",
                    "severity": "warning",
                    "message": f"Spoken backfill attempted but spoken channel is still missing ({reason_text}).",
                    "fallback_used": "screen_text_for_tts",
                }
            )

    display_elements = debug_info.get("display_elements")
    if isinstance(display_elements, Mapping):
        validation = display_elements.get("validation")
        if isinstance(validation, Mapping) and validation.get("valid") is False:
            issues.append(
                {
                    "category": "display_elements",
                    "code": "validation_failed",
                    "severity": "error",
                    "message": "Display element contract validation failed.",
                }
            )
            errors = validation.get("errors")
            if isinstance(errors, list):
                for error in errors:
                    if isinstance(error, str) and error.strip():
                        issues.append(
                            {
                                "category": "display_elements",
                                "code": "validation_error",
                                "severity": "error",
                                "message": f"Display element validation error: {error.strip()}",
                            }
                        )

    issues = _dedupe_turn_output_health_issues(issues)
    severity_rank = {"info": 1, "warning": 2, "error": 3, "fatal": 4}
    max_severity = max(
        (
            severity_rank.get(str(issue.get("severity") or "").strip(), 0)
            for issue in issues
        ),
        default=0,
    )
    status = "failed" if max_severity >= 3 else ("degraded" if max_severity else "ok")
    return {
        "schema_version": TURN_OUTPUT_HEALTH_SCHEMA_VERSION,
        "status": status,
        "issues": issues,
    }


def turn_output_health_issue_messages(debug_info: Mapping[str, Any]) -> list[str]:
    health = debug_info.get("turn_output_health")
    if not isinstance(health, Mapping):
        return []
    if health.get("schema_version") != TURN_OUTPUT_HEALTH_SCHEMA_VERSION:
        return []
    issues = health.get("issues")
    if not isinstance(issues, list):
        return []
    messages: list[str] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        message = issue.get("message")
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    return messages
