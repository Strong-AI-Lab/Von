from __future__ import annotations

import pytest

from src.backend.services.response_transformation_telemetry import (
    REQUIRED_TRANSFORMATION_EVENT_FIELDS,
    build_response_transformation_event,
    build_response_transformation_telemetry_payload,
    record_response_transformation_event,
    validate_response_transformation_event,
)


def test_build_response_transformation_event_contains_required_contract_fields() -> None:
    event = build_response_transformation_event(
        transform_name="buttonify",
        transform_version="v1",
        status="success",
        input_summary={"response_text_chars": 12},
        output_summary={"options": ["Proceed"]},
        options_emitted_count=1,
        source_path="heuristic_preflight",
        latency_ms=5.2,
        model_id=None,
        suppression_reason=None,
        error_class=None,
    )

    for field in REQUIRED_TRANSFORMATION_EVENT_FIELDS:
        assert field in event
    assert validate_response_transformation_event(event) == []


def test_validate_response_transformation_event_flags_missing_fields() -> None:
    errors = validate_response_transformation_event({"transform_name": "buttonify"})
    assert "missing_required_field:transform_version" in errors
    assert "missing_required_field:status" in errors
    assert "invalid_transform_version" in errors
    assert "invalid_status" in errors


def test_record_response_transformation_event_rejects_invalid_event() -> None:
    payload = build_response_transformation_telemetry_payload(request_id="req-1")
    valid_event = build_response_transformation_event(
        transform_name="screen_backfill",
        transform_version="v1",
        status="skipped",
        input_summary={"presenter_mode_requested": False},
        output_summary={"applied": False},
        options_emitted_count=0,
        source_path="none",
        latency_ms=0.1,
        model_id=None,
        suppression_reason="presenter_mode_disabled",
        error_class=None,
    )
    record_response_transformation_event(payload, event=valid_event)

    assert isinstance(payload.get("transformations"), list)
    assert len(payload["transformations"]) == 1

    with pytest.raises(ValueError):
        record_response_transformation_event(payload, event={"transform_name": "broken"})

