import io
from types import SimpleNamespace

import pytest
from PIL import Image, PngImagePlugin

from src.backend.services import participant_profile_service as profiles


def test_legacy_identity_header_cannot_authorise_profile_effects(monkeypatch):
    monkeypatch.setattr(profiles, "get_effective_user_concept_id", lambda: "#V#alice")
    monkeypatch.setattr(
        profiles,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#alice", "legacy_identity_header"),
    )
    with pytest.raises(PermissionError, match="authenticated participant"):
        profiles.set_avatar(remove=True)
    with pytest.raises(PermissionError, match="authenticated participant"):
        profiles.generate_avatar(prompt="avatar")


@pytest.mark.parametrize(
    "source",
    [
        "authenticated_session",
        "authenticated_session_derived",
        "trusted_in_process_actor",
    ],
)
def test_trusted_self_profile_effect_is_allowed(monkeypatch, source):
    monkeypatch.setattr(profiles, "get_effective_user_concept_id", lambda: "#V#alice")
    monkeypatch.setattr(
        profiles,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#alice", source),
    )
    monkeypatch.setattr(
        profiles.ConceptsRepository, "find_one", lambda q: {"concept_id": "#V#alice"}
    )
    assert profiles._participant(edit=True)[0] == "#V#alice"


def test_another_participant_cannot_be_edited(monkeypatch):
    monkeypatch.setattr(profiles, "get_effective_user_concept_id", lambda: "#V#alice")
    with pytest.raises(PermissionError):
        profiles.set_avatar(concept_id="#V#bob", data=b"not an image")
    with pytest.raises(PermissionError):
        profiles.generate_avatar(concept_id="#V#bob", prompt="avatar")


@pytest.mark.parametrize(
    "org,expected", [("#V#lab", "combined"), ("#V#elsewhere", "user"), (None, "user")]
)
def test_avatar_scope_precedence_and_context(monkeypatch, org, expected):
    monkeypatch.setattr(profiles, "get_effective_user_concept_id", lambda: "#V#alice")
    monkeypatch.setattr(profiles, "get_effective_organisation_concept_id", lambda: org)
    records = [
        {
            "concept_id": name,
            "attributes": {
                "avatar_subject_id": "#V#alice",
                "avatar_scope": scope,
                "user_concept_id": "#V#alice",
                "organisation_concept_id": "#V#lab",
            },
        }
        for name, scope in [
            ("global", "global_general"),
            ("org", "organisation_general"),
            ("user", "user_only_default"),
            ("combined", "user_org_default"),
        ]
    ]
    assert profiles._resolve_avatar(records, "#V#alice")["concept_id"] == expected


@pytest.mark.parametrize(
    "crop,orientation,colour",
    [
        (None, None, (255, 165, 0, 255)),
        ({"x": 400, "y": 0, "size": 200}, None, (0, 0, 255, 255)),
        ({"x": 0, "y": 400, "size": 200}, 6, (0, 0, 255, 255)),
    ],
)
def test_upload_publishes_only_sanitised_derivative_in_requested_scope(
    monkeypatch, crop, orientation, colour
):
    from src.backend.services import blob_uploads, computer_file_copy_service

    monkeypatch.setattr(
        profiles, "_participant", lambda *a, **k: ("#V#alice", "#V#alice", {})
    )
    monkeypatch.setattr(
        profiles, "get_effective_organisation_concept_id", lambda: "#V#lab"
    )
    monkeypatch.setattr(profiles, "_avatar_records", lambda ids: [])
    saved = {}

    def put(**kwargs):
        saved.update(kwargs)
        return SimpleNamespace(
            ref=SimpleNamespace(
                key="avatar/key", backend="local", uri="local:avatar/key"
            )
        )

    monkeypatch.setattr(blob_uploads, "put_bytes_durable", put)

    def create(**kwargs):
        saved["file"] = kwargs
        return SimpleNamespace(concept_id="#V#avatar_file")

    monkeypatch.setattr(
        computer_file_copy_service, "create_computer_file_copy_instance", create
    )
    monkeypatch.setattr(
        profiles.ConceptsRepository,
        "find_one",
        lambda q: {"attributes": {"sha256": saved["file"]["sha256"]}},
    )
    monkeypatch.setattr(profiles, "get_profile", lambda cid: {"concept_id": cid})
    image = Image.new("RGB", (600, 300), "orange")
    image.paste("blue", (400, 0, 600, 300))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-note", "private source information")
    source = io.BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    image.save(source, format="PNG", pnginfo=metadata, exif=exif)
    result = profiles.set_avatar(
        data=source.getvalue(), scope="organisation_general", crop=crop
    )
    derivative = Image.open(io.BytesIO(saved["data"]))
    assert derivative.size == (256, 256)
    assert "private-note" not in derivative.info
    assert not derivative.getexif()
    assert derivative.getpixel((128, 128)) == colour
    assert saved["file"]["visibility_scope_mode"] == "organisation_general"
    assert saved["file"]["metadata"]["avatar_subject_id"] == "#V#alice"
    assert result["saved_scope"] == "organisation_general"


def test_generation_creates_private_preview_without_publishing(monkeypatch):
    import base64

    from src.backend.languagemodels import openai_client

    calls = []

    class Client:
        def with_options(self, **options):
            assert options == {"max_retries": 0}
            return self

        @property
        def images(self):
            return self

        def generate(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"image").decode())]
            )

    monkeypatch.setattr(
        profiles, "_participant", lambda *a, **k: ("#V#alice", "#V#alice", {})
    )
    monkeypatch.setattr(
        openai_client, "OpenAIClient", lambda: SimpleNamespace(client=Client())
    )
    monkeypatch.setattr(profiles, "store_image", lambda **kwargs: kwargs)
    result = profiles.generate_avatar(prompt="A calm geometric fox")
    assert len(calls) == 1 and calls[0]["n"] == 1
    assert result["user_concept_id"] == "#V#alice"
    assert result["provenance"]["kind"] == "generated"


def test_read_avatar_requires_visible_published_record(monkeypatch):
    monkeypatch.setattr(
        profiles, "_participant", lambda *a, **k: ("#V#alice", "#V#bob", {})
    )
    monkeypatch.setattr(profiles, "_avatar_records", lambda ids: [])
    with pytest.raises(PermissionError):
        profiles.load_avatar("#V#bob")


def test_crop_is_applied_after_orientation_and_original_is_preserved(monkeypatch):
    from src.backend.services import avatar_framing

    source = io.BytesIO()
    image = Image.new("RGB", (400, 200), "red")
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, format="JPEG", exif=exif)
    original = source.getvalue()
    monkeypatch.setattr(
        profiles, "_participant", lambda *a, **k: ("#V#alice", "#V#alice", {})
    )
    stored = []
    monkeypatch.setattr(
        profiles,
        "store_image",
        lambda **kw: stored.append(kw) or {"concept_id": "#V#source"},
    )
    monkeypatch.setattr(avatar_framing, "find_faces", lambda img: [])
    result = profiles.prepare_avatar(data=original, filename="portrait.jpg")
    assert (result["width"], result["height"]) == (200, 400)
    assert result["crop"] == {"x": 0, "y": 100, "size": 200}
    assert stored[0]["data"] == original
    assert stored[0]["user_concept_id"] == "#V#alice"
    assert profiles._crop_box({"x": 0, "y": 200, "size": 200}, 200, 400) == (
        0,
        200,
        200,
        400,
    )


@pytest.mark.parametrize(
    "crop",
    [
        None,
        {},
        {"x": -1, "y": 0, "size": 10},
        {"x": 0, "y": 0, "size": 101},
        {"x": 0, "y": 0, "size": float("nan")},
        {"x": True, "y": 0, "size": 10},
    ],
)
def test_invalid_crop_is_rejected(crop):
    with pytest.raises(ValueError):
        profiles._crop_box(crop, 100, 100)


def test_photo_generation_sends_oriented_pixels_and_provenance_without_publication(
    monkeypatch,
):
    import base64

    from src.backend.languagemodels import openai_client

    original = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private note"
    Image.new("RGB", (120, 80), "blue").save(original, "JPEG", exif=exif)
    calls = []

    class Client:
        def with_options(self, **options):
            assert options == {"max_retries": 0}
            return self

        @property
        def images(self):
            return self

        def edit(self, **kwargs):
            image = Image.open(kwargs["image"])
            assert image.size == (80, 120)
            assert not image.getexif()
            calls.append(kwargs)
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"preview").decode())]
            )

        def generate(self, **kwargs):
            pytest.fail("Photo-conditioned generation must never become text-only")

    monkeypatch.setattr(
        profiles, "_participant", lambda *a, **k: ("#V#alice", "#V#alice", {})
    )

    def load(cid, actor):
        assert (cid, actor) == ("#V#source", "#V#alice")
        return {"sha256": "source-hash"}, original.getvalue()

    monkeypatch.setattr(profiles, "load_image", load)
    monkeypatch.setattr(
        openai_client, "OpenAIClient", lambda: SimpleNamespace(client=Client())
    )
    monkeypatch.setattr(profiles, "store_image", lambda **kwargs: kwargs)
    result = profiles.generate_avatar(
        prompt="Watercolour portrait", source_image_concept_id="#V#source"
    )
    assert len(calls) == 1
    assert result["provenance"]["parent_concept_id"] == "#V#source"
    assert result["provenance"]["parent_sha256"] == "source-hash"
    assert result["user_concept_id"] == "#V#alice"


def test_photo_generation_denies_foreign_source_before_provider(monkeypatch):
    from src.backend.languagemodels import openai_client

    monkeypatch.setattr(
        profiles, "_participant", lambda *a, **k: ("#V#alice", "#V#alice", {})
    )

    def denied(*args):
        raise PermissionError("Image unavailable")

    monkeypatch.setattr(profiles, "load_image", denied)
    monkeypatch.setattr(
        openai_client,
        "OpenAIClient",
        lambda: pytest.fail("No provider request permitted"),
    )
    with pytest.raises(PermissionError):
        profiles.generate_avatar(
            prompt="Portrait", source_image_concept_id="#V#bob_photo"
        )


@pytest.mark.parametrize(
    "kind,face_count", [("single", 1), ("multiple", 2), ("none", 0)]
)
def test_real_face_framing_and_recoverable_defaults(monkeypatch, kind, face_count):
    from pathlib import Path

    portrait = Image.open(
        Path(__file__).parents[1] / "fixtures/avatar/astronaut.png"
    ).convert("RGB")
    if kind == "multiple":
        image = Image.new("RGB", (1024, 512))
        image.paste(portrait, (0, 0))
        image.paste(portrait, (512, 0))
    elif kind == "none":
        image = Image.new("RGB", (800, 400), "green")
    else:
        image = portrait
    output = io.BytesIO()
    image.save(output, "PNG")
    monkeypatch.setattr(
        profiles, "_participant", lambda *a, **k: ("#V#alice", "#V#alice", {})
    )
    monkeypatch.setattr(
        profiles, "store_image", lambda **kw: {"concept_id": "#V#source"}
    )
    result = profiles.prepare_avatar(data=output.getvalue(), filename="fixture.png")
    assert result["face_detection_available"]
    assert result["face_count"] == face_count
    crop = result["crop"]
    profiles._crop_box(crop, *image.size)
    if kind == "single":
        assert 180 < crop["x"] + crop["size"] / 2 < 270
        assert 70 < crop["y"] + crop["size"] / 2 < 160
        assert crop["size"] < 250
    else:
        assert crop == {
            "x": (image.width - image.height) / 2,
            "y": 0,
            "size": image.height,
        }


def test_detector_failure_keeps_manual_upload_available(monkeypatch):
    from src.backend.services import avatar_framing

    monkeypatch.setattr(
        profiles, "_participant", lambda *a, **k: ("#V#alice", "#V#alice", {})
    )
    monkeypatch.setattr(
        profiles, "store_image", lambda **kw: {"concept_id": "#V#source"}
    )

    def unavailable(*a):
        raise ImportError("fixture unavailable")

    monkeypatch.setattr(avatar_framing, "find_faces", unavailable)
    output = io.BytesIO()
    Image.new("RGB", (100, 200)).save(output, "PNG")
    result = profiles.prepare_avatar(data=output.getvalue(), filename="photo.png")
    assert not result["face_detection_available"]
    assert result["crop"] == {"x": 0, "y": 50, "size": 100}


def test_prepare_requires_self_and_trusted_actor_before_storage(monkeypatch):
    monkeypatch.setattr(profiles, "get_effective_user_concept_id", lambda: "#V#alice")
    monkeypatch.setattr(
        profiles,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#alice", "authenticated_session"),
    )
    monkeypatch.setattr(
        profiles,
        "store_image",
        lambda **k: pytest.fail("No storage before authorisation"),
    )
    with pytest.raises(PermissionError):
        profiles.prepare_avatar(
            concept_id="#V#bob", data=b"photo", filename="photo.png"
        )
    monkeypatch.setattr(
        profiles,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#alice", "legacy_identity_header"),
    )
    with pytest.raises(PermissionError):
        profiles.prepare_avatar(data=b"photo", filename="photo.png")


def test_profile_http_routes_forward_source_crop_and_return_typed_failures(monkeypatch):
    from flask import Blueprint, Flask

    from src.backend.server.routes.participant_profile_routes import (
        register_participant_profile_routes,
    )

    app = Flask(__name__)
    blueprint = Blueprint("avatar_test", __name__)
    register_participant_profile_routes(blueprint)
    app.register_blueprint(blueprint, url_prefix="/von")
    calls = []
    monkeypatch.setattr(
        profiles,
        "prepare_avatar",
        lambda **kw: calls.append(kw) or {"image": {"concept_id": "#V#source"}},
    )
    monkeypatch.setattr(
        profiles,
        "generate_avatar",
        lambda **kw: calls.append(kw) or {"concept_id": "#V#derived"},
    )
    monkeypatch.setattr(
        profiles,
        "set_avatar",
        lambda **kw: calls.append(kw) or {"concept_id": "#V#alice"},
    )
    with app.test_client() as client:
        result = client.post(
            "/von/api/participants/avatar/prepare",
            data={
                "file": (io.BytesIO(b"photo"), "photo.png"),
                "concept_id": "#V#alice",
            },
        )
        assert result.status_code == 200
        assert calls[-1]["data"] == b"photo"
        result = client.post(
            "/von/api/participants/avatar/generate",
            json={
                "concept_id": "#V#alice",
                "source_image_concept_id": "#V#source",
                "prompt": "Watercolour",
            },
        )
        assert result.status_code == 200
        assert calls[-1]["source_image_concept_id"] == "#V#source"
        crop = {"x": 10, "y": 20, "size": 100}
        result = client.post(
            "/von/api/participants/avatar",
            json={
                "concept_id": "#V#alice",
                "image_concept_id": "#V#source",
                "crop": crop,
                "scope": "user_only_default",
            },
        )
        assert result.status_code == 200
        assert calls[-1]["crop"] == crop
        assert calls[-1]["scope"] == "user_only_default"
        result = client.post("/von/api/participants/avatar", data={"crop": "{invalid"})
        assert result.status_code == 400

        def unavailable(**kw):
            raise PermissionError("Image unavailable")

        monkeypatch.setattr(profiles, "generate_avatar", unavailable)
        assert (
            client.post(
                "/von/api/participants/avatar/generate",
                json={"prompt": "Portrait", "source_image_concept_id": "#V#foreign"},
            ).status_code
            == 403
        )
