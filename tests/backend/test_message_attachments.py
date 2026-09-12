"""Exact-message access, byte delivery and receiving-controller handoff checks."""

import hashlib
from pathlib import Path

import pytest

from src.backend.security.access_control import (
    get_effective_user_concept_id,
    override_current_actor,
)
from src.backend.services import conversation_image_service as images
from src.backend.services import message_attachment_service as attachments
from src.backend.services import message_service as messages


@pytest.fixture
def shared_file(monkeypatch):
    data = "Māori field notes: sample 42".encode()
    info = dict(
        concept_id="#V#file",
        filename="notes (original).txt",
        **images.inspect_attachment(data, "text/plain"),
        provenance={"kind": "user_upload"},
    )
    message = {
        "concept_id": "#V#message",
        "relationships": {"#V#has_sender": ["#V#alice"]},
        "concept_data": {"message_status": "sent", "metadata": {"attachments": [info]}},
    }
    monkeypatch.setattr(
        messages,
        "get_message_for_user",
        lambda mid, actor: (
            message if mid == "#V#message" and actor in ("#V#alice", "#V#bob") else None
        ),
    )
    calls = []

    def load(cid, actor):
        calls.append((cid, actor, get_effective_user_concept_id()))
        assert cid == "#V#file" and actor == "#V#alice"
        return info, data

    monkeypatch.setattr(images, "load_image", load)
    return message, info, data, calls


def test_recipient_reads_exact_copy_without_widening_file_audience(shared_file):
    message, info, data, calls = shared_file
    ref = attachments.message_attachment_descriptors(message)[0]
    with override_current_actor("#V#bob"):
        assert attachments.load_attachment_reference(ref, "#V#bob") == (info, data)
        assert get_effective_user_concept_id() == "#V#bob"
    assert calls == [("#V#file", "#V#alice", "#V#alice")]
    assert (
        messages.project_direct_message(message)["attachments"][0]["message_id"]
        == "#V#message"
    )


@pytest.mark.parametrize(
    "actor,mid,cid",
    [
        ("#V#eve", "#V#message", "#V#file"),
        ("#V#bob", "#V#other", "#V#file"),
        ("#V#bob", "#V#message", "#V#other"),
    ],
)
def test_nonparticipant_wrong_message_and_substituted_file_are_denied(
    shared_file, actor, mid, cid
):
    with pytest.raises(PermissionError):
        attachments.load_attachment_reference(
            {"message_id": mid, "concept_id": cid}, actor
        )
    assert not shared_file[3]


def test_deleted_message_revokes_recipient_read(shared_file):
    message, _, _, calls = shared_file
    message["concept_data"]["deleted"] = True
    with pytest.raises(PermissionError):
        attachments.load_attachment_reference(
            {"message_id": "#V#message", "concept_id": "#V#file"}, "#V#bob"
        )
    assert not calls


def test_text_provider_receives_content_not_just_filename(shared_file):
    message, _, data, _ = shared_file
    ref = attachments.message_attachment_descriptors(message)[0]
    with override_current_actor("#V#bob"):
        output = images.provider_image_messages(
            [{"role": "user", "content": "Summarise", "image_attachments": [ref]}],
            surface="chat",
        )
    assert data.decode() in output[0]["content"][0]["text"]
    assert "untrusted source data" in output[0]["content"][0]["text"]


def test_receiving_inbox_prepares_original_bytes_and_text(shared_file, tmp_path):
    from scripts.codex_von_inbox import prepare_attachment_inputs

    message, _, data, _ = shared_file
    context = {"message": messages.project_direct_message(message)}
    prepare_attachment_inputs(
        {"agent_id": "#V#bob", "organisation_id": "#V#org"}, context, tmp_path
    )
    item = context["attachment_inputs"][0]
    assert Path(item["local_path"]).read_bytes() == data
    assert item["sha256"] == hashlib.sha256(data).hexdigest()
    assert data.decode() in item["content"]
    assert Path(item["local_path"]).stat().st_mode & 0o777 == 0o600


def test_delivery_identity_binds_attachments_and_allows_attachment_only(shared_file):
    _, info, _, _ = shared_file
    kwargs = {
        "delivery_idempotency_key": "retry-key",
        "sender_id": "#V#alice",
        "recipient_ids": ["#V#bob"],
        "organisation_concept_id": "#V#org",
        "content": "",
    }
    first = messages.build_direct_message_delivery_identity(
        **kwargs, metadata={"attachments": [info]}
    )
    second = messages.build_direct_message_delivery_identity(
        **kwargs, metadata={"attachments": [{**info, "concept_id": "#V#other"}]}
    )
    assert first.idempotency_scope == second.idempotency_scope
    assert first.payload_fingerprint != second.payload_fingerprint


def test_unsupported_binary_and_truncated_text_are_explicit():
    info = dict(
        filename="report.pdf", **images.inspect_attachment(b"pdf", "application/pdf")
    )
    assert "interpretation unsupported" in attachments.attachment_text(info, b"pdf")
    info["content_type"] = "text/plain"
    assert "truncated" in attachments.attachment_text(info, b"a" * 25000)


def test_message_reference_hydrates_actual_image_for_recipient_provider(
    shared_file, monkeypatch
):
    import base64
    import io

    from PIL import Image

    message, _, _, _ = shared_file
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(stream, "PNG")
    data = stream.getvalue()
    info = dict(
        concept_id="#V#file",
        filename="diagram.png",
        **images.inspect_image(data),
        provenance={"kind": "user_upload"},
    )
    monkeypatch.setattr(images, "load_image", lambda cid, actor: (info, data))
    reference = attachments.message_attachment_descriptors(message)[0]
    with override_current_actor("#V#bob"):
        result = images.provider_image_messages(
            [
                {
                    "role": "user",
                    "content": "Read the image",
                    "image_attachments": [reference],
                }
            ],
            surface="responses",
        )
    assert (
        base64.b64decode(result[0]["content"][1]["image_url"].split(",", 1)[1]) == data
    )


def test_receiving_inbox_prepares_image_input_for_inspection(
    shared_file, monkeypatch, tmp_path
):
    from scripts.codex_von_inbox import prepare_attachment_inputs

    message, info, data, _ = shared_file
    # Delivery is byte-preserving; decoding/validation happens in the canonical loader.
    monkeypatch.setattr(
        images,
        "load_image",
        lambda cid, actor: ({**info, "content_type": "image/png"}, data),
    )
    context = {"message": messages.project_direct_message(message)}
    prepare_attachment_inputs(
        {"agent_id": "#V#bob", "organisation_id": "#V#org"}, context, tmp_path
    )
    item = context["attachment_inputs"][0]
    assert Path(item["local_path"]).read_bytes() == data
    assert item["local_path"].endswith(".png")
    assert "view_image" in item["instruction"]
