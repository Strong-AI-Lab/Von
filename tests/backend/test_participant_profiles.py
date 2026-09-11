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


def test_upload_publishes_only_sanitised_derivative_in_requested_scope(monkeypatch):
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
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-note", "private source information")
    source = io.BytesIO()
    image.save(source, format="PNG", pnginfo=metadata)
    result = profiles.set_avatar(data=source.getvalue(), scope="organisation_general")
    derivative = Image.open(io.BytesIO(saved["data"]))
    assert derivative.size == (256, 256)
    assert "private-note" not in derivative.info
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
