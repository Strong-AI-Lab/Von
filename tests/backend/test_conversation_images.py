"""Synthetic byte, authority and provider-boundary image checks."""

import base64
import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from src.backend.services import conversation_image_service as images
from src.backend.security.access_control import override_current_actor


def png():
    stream = io.BytesIO()
    Image.new("RGB", (96, 64), "navy").save(stream, "PNG")
    return stream.getvalue()


def test_upload_preserves_original_filename_but_uses_safe_blob_key(monkeypatch):
    from src.backend.services import blob_uploads, computer_file_copy_service
    from src.backend.db.repositories.concepts_repository import ConceptsRepository

    queries, blobs, records = [], [], []
    monkeypatch.setattr(
        ConceptsRepository, "find_one", lambda query, *a: queries.append(query)
    )

    def put(**kwargs):
        blobs.append(kwargs)
        return SimpleNamespace(
            ref=SimpleNamespace(backend="local", key=kwargs["key"], uri="blob:test")
        )

    def create(**kwargs):
        records.append(kwargs)
        return SimpleNamespace(concept_id="#V#original_name")

    monkeypatch.setattr(blob_uploads, "put_bytes_durable", put)
    monkeypatch.setattr(
        computer_file_copy_service, "create_computer_file_copy_instance", create
    )
    filename = "Māori diagram (original).png"
    result = images.store_image(
        data=png(), filename=filename, user_concept_id="#V#owner"
    )
    assert result["filename"] == filename
    assert records[0]["name"] == filename
    assert queries[0]["attributes.original_filename"] == filename
    assert blobs[0]["metadata"]["original_filename"] == filename
    assert blobs[0]["key"].endswith("Maori_diagram_original.png")


def test_detects_actual_media_and_rejects_corrupt_oversized_and_animated():
    data = png()
    info = images.inspect_image(data)
    assert info["content_type"] == "image/png"
    assert (info["width"], info["height"]) == (96, 64)
    with pytest.raises(ValueError):
        images.inspect_image(b"<svg onload='attack()'>")
    with pytest.raises(ValueError):
        images.inspect_image(b"x" * (images.MAX_IMAGE_BYTES + 1))
    stream = io.BytesIO()
    Image.new("RGB", (10, 10)).save(stream, "GIF")
    with pytest.raises(ValueError):
        images.inspect_image(stream.getvalue())


@pytest.mark.parametrize("surface", ["chat", "responses", "ollama"])
def test_provider_receives_original_bytes_and_retained_context_has_no_base64(
    monkeypatch, surface
):
    data = png()
    descriptor = {"concept_id": "#V#synthetic_image", **images.inspect_image(data)}
    calls = []

    def load(cid, actor):
        calls.append((cid, actor))
        return descriptor, data

    monkeypatch.setattr(images, "load_image", load)
    context = [
        {
            "role": "user",
            "content": "Read this diagram",
            "image_attachments": [descriptor],
        }
    ]
    with override_current_actor("#V#owner", None):
        sent = images.provider_image_messages(context, surface=surface)
    if surface == "ollama":
        encoded = sent[0]["images"][0]
    else:
        block = sent[0]["content"][1]
        value = block["image_url"]
        encoded = (value["url"] if isinstance(value, dict) else value).split(",", 1)[1]
    assert base64.b64decode(encoded) == data
    assert calls == [(descriptor["concept_id"], "#V#owner")]
    assert "image_attachments" not in sent[0]
    assert context[0]["image_attachments"] == [descriptor]
    assert base64.b64encode(data).decode() not in str(context)


def test_wrong_actor_denied_before_blob_read(monkeypatch):
    from src.backend.services import computer_file_copy_service as files

    monkeypatch.setattr(
        files,
        "_load_file_copy_concept_doc",
        lambda **kw: {
            "attributes": {"conversation_image": True, "user_concept_id": "#V#owner"}
        },
    )
    monkeypatch.setattr(
        files,
        "fetch_file_copy_bytes",
        lambda **kw: pytest.fail("must not read private blob"),
    )
    with pytest.raises(PermissionError):
        images.load_image("#V#image", "#V#other")
    with pytest.raises(PermissionError):
        images.load_image("#V#image", None)


def test_revoked_archive_binding_denies_staged_copy_and_generic_file_route(monkeypatch):
    from src.backend.services import computer_file_copy_service as files
    from src.backend.integrations.internal_mcp import otter_archive_proxy_mcp as archive

    monkeypatch.setattr(
        files,
        "_load_file_copy_concept_doc",
        lambda **kw: {
            "attributes": {
                "conversation_image": True,
                "user_concept_id": "#V#owner",
                "image_provenance": {"kind": "otter_archive", "resource_id": "archive"},
            }
        },
    )
    monkeypatch.setattr(
        archive, "otter_archive_resource_binding_for_user", lambda actor: None
    )
    monkeypatch.setattr(
        files,
        "_file_copy_visible_to_actor",
        lambda **kw: pytest.fail("must deny before generic visibility or blob access"),
    )
    with pytest.raises(PermissionError):
        images.load_image("#V#image", "#V#owner")
    assert (
        files.fetch_file_copy_bytes(
            file_copy_concept_id="#V#image", user_concept_id="#V#owner"
        )["success"]
        is False
    )


def test_corrupt_original_is_not_delivered(monkeypatch):
    from src.backend.services import computer_file_copy_service as files

    monkeypatch.setattr(
        files,
        "_load_file_copy_concept_doc",
        lambda **kw: {
            "attributes": {
                "conversation_image": True,
                "user_concept_id": "#V#owner",
                "sha256": "bad",
            }
        },
    )
    monkeypatch.setattr(
        files,
        "fetch_file_copy_bytes",
        lambda **kw: {
            "success": True,
            "data": png(),
            "info": SimpleNamespace(original_filename="fixture.png"),
        },
    )
    with pytest.raises(ValueError, match="checksum"):
        images.load_image("#V#image", "#V#owner")


def test_multiple_descriptors_reauthorised_and_bounded(monkeypatch):
    monkeypatch.setattr(
        images, "load_image", lambda cid, actor: ({"concept_id": cid}, png())
    )
    assert images.authorise_images(["a", "b", "a"], "owner") == [
        {"concept_id": "a"},
        {"concept_id": "b"},
    ]
    with pytest.raises(ValueError):
        images.authorise_images([str(i) for i in range(9)], "owner")


def test_openai_context_and_continuation_keep_attachment_descriptors():
    from src.backend.languagemodels.structured_tool_calling.providers.openai_client import (
        OpenAIClient,
    )

    msg = {
        "role": "user",
        "content": "source",
        "image_attachments": [{"concept_id": "#V#image"}],
    }
    assert (
        OpenAIClient._chat_context_message(msg)["image_attachments"]
        == msg["image_attachments"]
    )
    assert (
        OpenAIClient._responses_context_message_item(msg)["image_attachments"]
        == msg["image_attachments"]
    )


def test_crop_preserves_source_restriction_original_hash_and_region(monkeypatch):
    data = png()
    info = {
        **images.inspect_image(data),
        "provenance": {
            "kind": "otter_archive",
            "resource_id": "private",
            "artifact_id": "original",
        },
    }
    monkeypatch.setattr(images, "load_image", lambda cid, actor: (info, data))
    stored = []
    monkeypatch.setattr(images, "store_image", lambda **kw: stored.append(kw) or kw)
    result = images.crop_image(
        concept_id="#V#original",
        user_concept_id="#V#owner",
        x=5,
        y=10,
        width=30,
        height=20,
    )
    assert images.inspect_image(result["data"])["width"] == 60
    assert result["provenance"] == {
        **info["provenance"],
        "parent_concept_id": "#V#original",
        "parent_sha256": info["sha256"],
        "transform": {"kind": "crop_resize", "box": [5, 10, 30, 20], "scale": 2},
    }
    with pytest.raises(ValueError):
        images.crop_image(
            concept_id="#V#original",
            user_concept_id="#V#owner",
            x=90,
            y=0,
            width=30,
            height=20,
        )
    assert len(stored) == 1


def test_attribute_metadata_reads_without_relation_fanout(monkeypatch):
    from src.backend.services import computer_file_copy_service as files

    monkeypatch.setattr(
        files,
        "_first_text_value",
        lambda *a: pytest.fail(
            "attribute-backed images must not fan out to text metadata reads"
        ),
    )
    attrs = {
        "file_copy_metadata_storage": "attributes.v1",
        "blob_key": "private/test.png",
        "blob_backend": "local",
        "blob_uri": "blob://test",
        "original_filename": "test.png",
        "content_type": "image/png",
        "size_bytes": 50,
    }
    record = files.resolve_file_copy_blob_info(
        file_copy_concept_id="#V#image", concept_doc={"attributes": attrs}
    )
    assert record.original_filename == "test.png"
    assert record.blob_key == "private/test.png"


def test_original_route_requires_actor_and_rechecks_original_bytes(monkeypatch):
    from flask import Blueprint, Flask
    from src.backend.server.routes.conversation_image_routes import (
        register_image_routes,
    )

    data = png()

    def load(cid, actor):
        if actor != "#V#owner":
            raise PermissionError()
        return {**images.inspect_image(data), "filename": "fixture.png"}, data

    monkeypatch.setattr(images, "load_image", load)
    app = Flask(__name__)
    bp = Blueprint("image_test", __name__)
    register_image_routes(bp)
    app.register_blueprint(bp, url_prefix="/von")
    client = app.test_client()
    assert client.get("/von/api/images/id/original").status_code == 401
    with override_current_actor("#V#other", None):
        assert client.get("/von/api/images/id/original").status_code == 404
    with override_current_actor("#V#owner", None):
        response = client.get("/von/api/images/id/original")
    assert response.status_code == 200 and response.data == data
    assert response.headers["Cache-Control"] == "private, no-store"


def test_history_projection_retains_attachment_association_without_debug():
    from src.backend.services.chat_history_service import (
        _split_history_into_segments_with_locations,
    )

    first = {
        "role": "user",
        "content": "Compare these",
        "image_attachments": [{"concept_id": "#V#a"}, {"concept_id": "#V#b"}],
    }
    second = {"role": "user", "content": "Follow up"}
    projected = _split_history_into_segments_with_locations(
        [first, second], session_id="session", include_debug=False
    )[0]
    assert projected[0]["image_attachments"] == first["image_attachments"]
    assert "image_attachments" not in projected[1]


def test_archive_resource_citations_resolve_only_to_authenticated_source_route():
    from src.backend.services.markdown_render_service import render_markdown_to_safe_html
    from bs4 import BeautifulSoup

    artifact = "a" * 64
    html = render_markdown_to_safe_html(f"[Source](otter-archive://artifact/{artifact}) [Invalid](otter-archive://other/private)")
    links = BeautifulSoup(html, "html.parser").find_all("a")
    assert links[0]["href"] == "/von/api/otter-archive/artifacts/" + artifact
    assert not links[1].has_attr("href")
