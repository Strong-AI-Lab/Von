from __future__ import annotations

from src.backend.services.display_elements_service import (
    build_turn_display_elements,
    validate_turn_display_elements,
)


def test_build_turn_display_elements_includes_screen_and_spoken_blocks() -> None:
    contract = build_turn_display_elements(
        response_text="Fallback response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "On-screen text",
            "spoken": "Talk track",
        },
    )

    assert contract["schema_version"] == "turn_display_elements_v1"
    assert contract["validation"]["valid"] is True
    elements = contract["elements"]
    assert isinstance(elements, list)

    screen = next(item for item in elements if item["element_id"] == "screen_text")
    spoken = next(item for item in elements if item["element_id"] == "spoken_text")
    assert screen["payload"]["text"] == "On-screen text"
    assert spoken["payload"]["text"] == "Talk track"
    assert contract["reason_codes"] == []


def test_build_turn_display_elements_appends_required_json_fence() -> None:
    required_fence = "```json\n{\"sentinel\": \"required\"}\n```"
    contract = build_turn_display_elements(
        response_text="Screen text without json block",
        presenter_channels={
            "format": "screen_backfill_from_response_v1",
            "screen": "Screen text without json block",
            "spoken": None,
        },
        required_screen_json_fence=required_fence,
        screen_backfill_second_pass_attempted=True,
        screen_backfill_second_pass_reason="missing_screen_fence",
    )

    assert contract["validation"]["valid"] is True
    json_blocks = [
        item for item in contract["elements"] if item["element_type"] == "json_block"
    ]
    assert len(json_blocks) == 1
    assert json_blocks[0]["payload"]["fence"] == required_fence
    assert "screen_backfill:missing_screen_fence" in contract["reason_codes"]
    assert "required_screen_json_fence_appended" in contract["reason_codes"]


def test_validate_turn_display_elements_rejects_unknown_element_type() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "bad",
                    "element_type": "unsupported_kind",
                    "channel": "screen",
                    "order": 10,
                    "intent": "primary_response",
                    "payload": {"text": "Hello"},
                    "provenance": {"source": "test"},
                }
            ],
            "reason_codes": [],
        }
    )

    assert valid is False
    assert any("unsupported_kind" in message for message in errors)


def test_build_turn_display_elements_orders_elements_deterministically() -> None:
    required_fence = "```json\n{\"sentinel\": \"required\"}\n```"
    contract = build_turn_display_elements(
        response_text=(
            "Screen content\n"
            "```json\n"
            "{\"existing\": true}\n"
            "```"
        ),
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": (
                "Screen content\n"
                "```json\n"
                "{\"existing\": true}\n"
                "```"
            ),
            "spoken": "Talk track",
        },
        required_screen_json_fence=required_fence,
    )

    element_ids = [element["element_id"] for element in contract["elements"]]
    assert element_ids == [
        "screen_text",
        "screen_json_block_1",
        "screen_json_block_2",
        "spoken_text",
    ]

    orders = [element["order"] for element in contract["elements"]]
    assert orders == sorted(orders)
