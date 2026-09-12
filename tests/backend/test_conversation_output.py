"""Deterministic transport, retention, ordering, and private output boundaries."""

import base64
import io
import json

import pytest
from PIL import Image

from src.backend.services import conversation_image_service as images
from src.backend.services import conversation_output_service as output
from src.backend.services.display_elements_service import (
    build_turn_display_elements,
    validate_turn_display_elements,
)


def png(colour="navy"):
    stream = io.BytesIO()
    Image.new("RGB", (96, 64), colour).save(stream, "PNG")
    return stream.getvalue()


def response(*, mixed=False, status="completed", result=None):
    image = {
        "type": "image_generation_call",
        "id": "ig_fixture",
        "status": status,
        "result": result if result is not None else base64.b64encode(png()).decode(),
        "revised_prompt": "private provider prompt",
        "output_format": "png",
    }
    message = lambda ident, text: {
        "type": "message",
        "id": ident,
        "content": [{"type": "output_text", "text": text}],
    }
    return {
        "id": "resp_fixture",
        "model": "test-vision-model",
        "output": [message("before", "Before"), image, message("after", "After")]
        if mixed
        else [image],
    }


@pytest.fixture
def retained_store(monkeypatch):
    stored = {}

    def store(**kwargs):
        assert kwargs["user_concept_id"] == "#V#owner"
        info = images.inspect_image(kwargs["data"])
        identity = json.dumps([info["sha256"], kwargs["provenance"]], sort_keys=True)
        if identity not in stored:
            stored[identity] = {
                **info,
                "concept_id": f"#V#retained-{len(stored)}",
                "filename": kwargs["filename"],
                "provenance": kwargs["provenance"],
            }
        return stored[identity]

    monkeypatch.setattr(images, "store_image", store)
    return stored


def test_mixed_output_retains_order_pixels_identity_and_private_provenance(
    retained_store,
):
    parts = output.openai_visible_parts(response(mixed=True))
    assert [p.kind for p in parts] == ["text", "image", "text"]
    assert parts[1].image_data == png()
    assert "image_data" not in repr(parts[1])
    retained = output.retain_parts(parts, actor="#V#owner", turn_id="turn1")
    repeated = output.retain_parts(parts, actor="#V#owner", turn_id="turn1")
    assert retained == repeated
    assert len(retained_store) == 1
    contract = build_turn_display_elements(
        response_text="BeforeAfter",
        presenter_channels={"screen": "A paraphrase"},
        content_parts=retained,
    )
    assert validate_turn_display_elements(contract) == (True, [])
    visible = [
        e
        for e in contract["elements"]
        if e["provenance"].get("source") == "retained_content"
    ]
    assert [e["element_type"] for e in visible] == ["text_block", "image", "text_block"]
    assert (
        visible[1]["payload"]["asset"]["sha256"]
        == images.inspect_image(png())["sha256"]
    )
    serialised = json.dumps(
        [retained, contract, output.without_image_bytes(response())]
    )
    assert base64.b64encode(png()).decode() not in serialised
    assert "private provider prompt" not in json.dumps(contract)
    assert (
        next(iter(retained_store.values()))["provenance"]["response_id"]
        == "resp_fixture"
    )


@pytest.mark.parametrize(
    "item",
    [
        response(status="generating"),
        response(status="failed"),
        response(result="bad-data"),
    ],
)
def test_incomplete_or_invalid_image_is_local_failure(item):
    parts = output.openai_visible_parts(item)
    assert parts[0].kind == "media_error"
    assert parts[0].image_data is None


def test_storage_failure_keeps_useful_text_and_never_reissues_generation(monkeypatch):
    calls = []

    def fail(**kw):
        calls.append(kw)
        raise OSError("private storage details")

    monkeypatch.setattr(images, "store_image", fail)
    parts = output.retain_parts(
        output.openai_visible_parts(response(mixed=True)),
        actor="#V#owner",
        turn_id="failed",
    )
    assert [p["kind"] for p in parts] == ["text", "media_error", "text"]
    assert len(calls) == 1
    assert parts[1]["error_code"] == "image_storage_failed"
    assert "private storage details" not in str(parts)


@pytest.mark.parametrize("embedded", [False, True])
def test_tool_content_reuses_retention_and_removes_bytes(retained_store, embedded):
    encoded = base64.b64encode(png()).decode()
    media = (
        {
            "type": "resource",
            "resource": {
                "uri": "resource://fixture",
                "mimeType": "image/png",
                "blob": encoded,
            },
        }
        if embedded
        else {"type": "image", "mimeType": "image/png", "data": encoded}
    )
    result = output.retain_tool_images(
        {"content": [{"type": "text", "text": "Source"}, media]},
        actor="#V#owner",
        turn_id="tool-turn",
        tool_name="fixture",
        call_id="call1",
    )
    assert (
        result["image_attachments"][0]["sha256"]
        == images.inspect_image(png())["sha256"]
    )
    assert encoded not in json.dumps(result)
    assert [p["kind"] for p in result["content_parts"]] == ["text", "image"]


def test_mime_mismatch_and_non_image_resource_do_not_fetch():
    with pytest.raises(ValueError, match="declared"):
        output.decode_image(base64.b64encode(png()).decode(), "image/jpeg")
    payload = {
        "content": [
            {
                "type": "resource_link",
                "uri": "http://internal.invalid/private",
                "mimeType": "image/png",
            }
        ]
    }
    assert (
        output.retain_tool_images(
            payload, actor="#V#owner", turn_id="t", tool_name="x", call_id="c"
        )
        is payload
    )


def test_unknown_format_retains_identifiable_fallback():
    contract = build_turn_display_elements(
        response_text="",
        presenter_channels=None,
        content_parts=[{"kind": "future_canvas", "part_id": "x", "version": 8}],
    )
    assert validate_turn_display_elements(contract) == (True, [])
    assert "future_canvas" in contract["elements"][0]["payload"]["text"]


def test_repeated_element_id_emits_one_image(retained_store):
    parts = output.retain_parts(
        output.openai_visible_parts(response()), actor="#V#owner", turn_id="turn1"
    )
    contract = build_turn_display_elements(
        response_text="", presenter_channels=None, content_parts=parts + parts
    )
    assert len([e for e in contract["elements"] if e["element_type"] == "image"]) == 1


def test_history_restores_mixed_output_without_private_debug_and_edit_receives_pixels(
    retained_store, monkeypatch
):
    from src.backend.security.access_control import override_current_actor
    from src.backend.server.routes.generate_route_support import (
        _persist_generate_turn_messages,
    )
    from src.backend.services.chat_history_service import (
        _split_history_into_segments_with_locations,
    )

    parts = output.retain_parts(
        output.openai_visible_parts(response(mixed=True)),
        actor="#V#owner",
        turn_id="turn1",
    )
    history = []
    _persist_generate_turn_messages(
        history_user_id="#V#owner",
        user_message_persisted_early=True,
        prompt_text="Draw a scene",
        user_concept_id="#V#owner",
        session_id="visual-session",
        tool_messages=[],
        response_text="Before\nAfter",
        llm_debug_info={"request_id": "turn1"},
        user_namespace="#V#owner",
        org_concept_id=None,
        role_in_org=None,
        current_context=[],
        truncate_large_tool_results_fn=lambda messages, **kw: messages,
        add_chat_history_message_fn=lambda **kw: history.append(kw["message"]),
        limit_context_size_fn=lambda messages, **kw: messages,
        assistant_content_parts=parts,
    )
    restored = _split_history_into_segments_with_locations(
        history, session_id="visual-session", include_debug=False
    )[0][0]
    assert restored["content_parts"] == parts
    assert [e["element_type"] for e in restored["display_elements"]["elements"]] == [
        "text_block",
        "image",
        "text_block",
    ]
    assert "llm_debug_data" not in restored
    assert base64.b64encode(png()).decode() not in json.dumps(history)

    asset = restored["image_attachments"][0]
    loads = []

    def load(cid, actor):
        loads.append((cid, actor))
        return asset, png()

    monkeypatch.setattr(images, "load_image", load)
    with override_current_actor("#V#owner", None):
        sent = images.provider_image_messages(
            [restored, {"role": "user", "content": "Make the background green"}],
            surface="responses",
        )
    encoded = sent[0]["content"][1]["image_url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == png()
    assert loads == [(asset["concept_id"], "#V#owner")]

    edit_response = response(result=base64.b64encode(png("green")).decode())
    edit_response["id"] = "resp-edit"
    edit_response["output"][0].update(id="ig-edit", action="edit")
    edited = output.retain_parts(
        output.openai_visible_parts(edit_response),
        actor="#V#owner",
        turn_id="turn2",
        parent_ids=[asset["concept_id"]],
    )
    assert edited[0]["asset"]["concept_id"] != asset["concept_id"]
    assert edited[0]["asset"]["provenance"]["parent_concept_ids"] == [
        asset["concept_id"]
    ]
    assert restored["image_attachments"][0] == asset
    assert len(retained_store) == 2


def test_non_edit_output_records_inputs_without_claiming_a_revision(retained_store):
    parts = output.retain_parts(
        output.openai_visible_parts(response()),
        actor="#V#owner",
        turn_id="turn",
        parent_ids=["#V#source"],
    )
    provenance = parts[0]["asset"]["provenance"]
    assert provenance["input_concept_ids"] == ["#V#source"]
    assert "parent_concept_ids" not in provenance


def test_mcp_client_preserves_media_after_leading_text(retained_store):
    from mcp import types

    from src.backend.integrations.internal_mcp.mcp_proxy_base import MCPStdIOClient

    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="A tool-created image."),
            types.ImageContent(
                type="image",
                mimeType="image/png",
                data=base64.b64encode(png()).decode(),
            ),
        ]
    )
    client = object.__new__(MCPStdIOClient)
    payload, parse_method = client._parse_result_with_telemetry(result)
    assert parse_method == "media_content"
    retained = output.retain_tool_images(
        payload,
        actor="#V#owner",
        turn_id="mcp",
        tool_name="fixture",
        call_id="mcp-image",
    )
    assert (
        retained["image_attachments"][0]["sha256"]
        == images.inspect_image(png())["sha256"]
    )
    assert base64.b64encode(png()).decode() not in json.dumps(retained)


def test_invalid_mcp_image_type_is_a_local_failure_without_base64_leakage():
    encoded = base64.b64encode(png()).decode()
    result = output.retain_tool_images(
        {"content": [{"type": "image", "mimeType": "text/html", "data": encoded}]},
        actor="#V#owner",
        turn_id="t",
        tool_name="fixture",
        call_id="mcp-invalid",
    )
    assert result["content_parts"][0]["kind"] == "media_error"
    assert encoded not in json.dumps(result)
