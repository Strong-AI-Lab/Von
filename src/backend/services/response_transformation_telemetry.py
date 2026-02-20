"""Canonical telemetry contract for response transformation stages.

This module is the single authority for response transformation telemetry shape.
Keep all transformation-stage emitters routed through this helper so contract
changes happen in one place and remain backwards-auditable across turns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping

RESPONSE_TRANSFORMATION_TELEMETRY_SCHEMA_VERSION = "response_transformations_v1"
RESPONSE_TRANSFORMATION_EVENT_SCHEMA_VERSION = "response_transformation_event_v1"

REQUIRED_TRANSFORMATION_EVENT_FIELDS = (
    "transform_name",
    "transform_version",
    "status",
    "input_summary",
    "output_summary",
    "options_emitted_count",
    "source_path",
    "latency_ms",
    "model_id",
    "suppression_reason",
    "error_class",
    "timestamp_utc",
)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalise_timestamp(value: str | None) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _now_utc_iso()


def _normalise_latency_ms(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value < 0:
            return 0.0
        return float(value)
    return None


def _normalise_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


def build_response_transformation_telemetry_payload(
    *, request_id: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": RESPONSE_TRANSFORMATION_TELEMETRY_SCHEMA_VERSION,
        "event_schema_version": RESPONSE_TRANSFORMATION_EVENT_SCHEMA_VERSION,
        "generated_at_utc": _now_utc_iso(),
        "transformations": [],
    }
    if isinstance(request_id, str) and request_id.strip():
        payload["request_id"] = request_id.strip()
    return payload


def validate_response_transformation_event(event: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []

    for field in REQUIRED_TRANSFORMATION_EVENT_FIELDS:
        if field not in event:
            errors.append(f"missing_required_field:{field}")

    transform_name = event.get("transform_name")
    if not isinstance(transform_name, str) or not transform_name.strip():
        errors.append("invalid_transform_name")

    transform_version = event.get("transform_version")
    if not isinstance(transform_version, str) or not transform_version.strip():
        errors.append("invalid_transform_version")

    status = event.get("status")
    if not isinstance(status, str) or not status.strip():
        errors.append("invalid_status")

    input_summary = event.get("input_summary")
    if not isinstance(input_summary, Mapping):
        errors.append("invalid_input_summary")

    output_summary = event.get("output_summary")
    if not isinstance(output_summary, Mapping):
        errors.append("invalid_output_summary")

    options_emitted_count = event.get("options_emitted_count")
    if not isinstance(options_emitted_count, int) or options_emitted_count < 0:
        errors.append("invalid_options_emitted_count")

    source_path = event.get("source_path")
    if not isinstance(source_path, str) or not source_path.strip():
        errors.append("invalid_source_path")

    latency_ms = event.get("latency_ms")
    if latency_ms is not None and not isinstance(latency_ms, (int, float)):
        errors.append("invalid_latency_ms")

    model_id = event.get("model_id")
    if model_id is not None and not isinstance(model_id, str):
        errors.append("invalid_model_id")

    suppression_reason = event.get("suppression_reason")
    if suppression_reason is not None and not isinstance(suppression_reason, str):
        errors.append("invalid_suppression_reason")

    error_class = event.get("error_class")
    if error_class is not None and not isinstance(error_class, str):
        errors.append("invalid_error_class")

    timestamp_utc = event.get("timestamp_utc")
    if not isinstance(timestamp_utc, str) or not timestamp_utc.strip():
        errors.append("invalid_timestamp_utc")

    return errors


def build_response_transformation_event(
    *,
    transform_name: str,
    transform_version: str,
    status: str,
    input_summary: Mapping[str, Any] | None,
    output_summary: Mapping[str, Any] | None,
    options_emitted_count: Any,
    source_path: str | None,
    latency_ms: Any,
    model_id: str | None,
    suppression_reason: str | None,
    error_class: str | None,
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "transform_name": str(transform_name).strip(),
        "transform_version": str(transform_version).strip(),
        "status": str(status).strip(),
        "input_summary": dict(input_summary) if isinstance(input_summary, Mapping) else {},
        "output_summary": (
            dict(output_summary) if isinstance(output_summary, Mapping) else {}
        ),
        "options_emitted_count": _normalise_count(options_emitted_count),
        "source_path": (
            str(source_path).strip()
            if isinstance(source_path, str) and source_path.strip()
            else "none"
        ),
        "latency_ms": _normalise_latency_ms(latency_ms),
        "model_id": (
            str(model_id).strip()
            if isinstance(model_id, str) and model_id.strip()
            else None
        ),
        "suppression_reason": (
            str(suppression_reason).strip()
            if isinstance(suppression_reason, str) and suppression_reason.strip()
            else None
        ),
        "error_class": (
            str(error_class).strip()
            if isinstance(error_class, str) and error_class.strip()
            else None
        ),
        "timestamp_utc": _normalise_timestamp(timestamp_utc),
    }

    errors = validate_response_transformation_event(event)
    if errors:
        raise ValueError(
            "invalid_response_transformation_event:"
            + ",".join(sorted(set(errors)))
        )

    return event


def record_response_transformation_event(
    payload: MutableMapping[str, Any], *, event: Mapping[str, Any]
) -> dict[str, Any]:
    transformations = payload.get("transformations")
    if not isinstance(transformations, list):
        raise ValueError("invalid_response_transformation_payload:transformations")

    event_dict = dict(event)
    errors = validate_response_transformation_event(event_dict)
    if errors:
        raise ValueError(
            "invalid_response_transformation_event:" + ",".join(sorted(set(errors)))
        )

    transformations.append(event_dict)
    payload["generated_at_utc"] = _now_utc_iso()
    return event_dict

