"""Participant presentation, independent of conversation transport.

The concept remains the identity authority. An avatar is an explicitly published,
metadata-free derivative with the concept's audience, never a private image URL.
"""

from __future__ import annotations

import base64
import hashlib
import io
from urllib.parse import quote

from PIL import Image, ImageOps

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import (
    AUTHENTICATED_SESSION_ACTOR_SOURCE,
    AUTHENTICATED_SESSION_DERIVED_ACTOR_SOURCE,
    TRUSTED_IN_PROCESS_ACTOR_SOURCE,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
    get_effective_user_concept_id_with_source,
)
from .conversation_image_service import inspect_image, load_image, store_image


def _participant(concept_id: str | None = None, *, edit: bool = False):
    actor = get_effective_user_concept_id()
    if edit and not _can_edit_profile(actor):
        raise PermissionError(
            "An authenticated participant is required to change a profile."
        )
    concept_id = concept_id or actor
    if not actor or not concept_id or (edit and concept_id != actor):
        raise PermissionError(
            "Only the signed-in participant can change their profile."
        )
    doc = ConceptsRepository.find_one({"concept_id": concept_id})
    if not doc:
        raise PermissionError("Participant unavailable.")
    return actor, concept_id, doc


def _can_edit_profile(actor):
    resolved, source = get_effective_user_concept_id_with_source()
    return bool(
        actor
        and resolved == actor
        and source
        in {
            AUTHENTICATED_SESSION_ACTOR_SOURCE,
            AUTHENTICATED_SESSION_DERIVED_ACTOR_SOURCE,
            TRUSTED_IN_PROCESS_ACTOR_SOURCE,
        }
    )


SCOPES = (
    "user_org_default",
    "user_only_default",
    "organisation_general",
    "global_general",
)


def _avatar_records(concept_ids):
    # Scope is enforced by the same canonical concept visibility mechanism as
    # other Von files. Never embed private variants in the public subject.
    return list(
        ConceptsRepository.find(
            {
                "attributes.avatar_subject_id": {"$in": concept_ids},
                "attributes.avatar_retired": {"$ne": True},
            },
            sort=[("created_at", -1)],
        )
    )


def _resolve_avatar(records, concept_id):
    actor = get_effective_user_concept_id()
    org = get_effective_organisation_concept_id()
    candidates = []
    for record in records:
        attrs = record.get("attributes") or {}
        if attrs.get("avatar_subject_id") != concept_id:
            continue
        scope = attrs.get("avatar_scope")
        if scope not in SCOPES:
            continue
        if (
            scope in ("organisation_general", "user_org_default")
            and attrs.get("organisation_concept_id") != org
        ):
            continue
        if scope == "user_only_default" and attrs.get("user_concept_id") != actor:
            continue
        candidates.append(record)
    # Source order breaks ties by newest publication within the selected scope.
    return (
        min(candidates, key=lambda r: SCOPES.index(r["attributes"]["avatar_scope"]))
        if candidates
        else None
    )


def get_profiles(concept_ids: list[str]) -> list[dict]:
    actor = get_effective_user_concept_id()
    if not actor:
        raise PermissionError("Authentication required.")
    if (
        not isinstance(concept_ids, list)
        or len(concept_ids) > 100
        or any(not isinstance(cid, str) for cid in concept_ids)
    ):
        raise ValueError("Supply at most 100 participant IDs.")
    docs = list(
        ConceptsRepository.find(
            {"concept_id": {"$in": list(dict.fromkeys(concept_ids))}},
            projection={"concept_id": 1, "name": 1, "names": 1, "relationships": 1},
        )
    )
    from .concept_service import resolve_concept_display_names

    names = resolve_concept_display_names(docs)
    records = _avatar_records([doc["concept_id"] for doc in docs])
    result = []
    for doc in docs:
        cid = doc["concept_id"]
        record = _resolve_avatar(records, cid)
        avatar = (record or {}).get("attributes") or {}
        result.append(
            {
                "concept_id": cid,
                "display_name": names.get(cid)
                or cid.removeprefix("#V#").replace("_", " "),
                "can_edit": actor == cid and _can_edit_profile(actor),
                "avatar_scope": avatar.get("avatar_scope"),
                "avatar_url": f"/von/api/participants/{quote(cid, safe='')}/avatar?v={avatar['sha256']}"
                if avatar.get("sha256")
                else None,
                "avatar_version": avatar.get("sha256"),
                "available_scopes": list(SCOPES)
                if get_effective_organisation_concept_id()
                else ["user_only_default", "global_general"],
            }
        )
    return result


def get_profile(concept_id: str | None = None) -> dict:
    _, concept_id, _ = _participant(concept_id)
    profiles = get_profiles([concept_id])
    if not profiles:
        raise PermissionError("Participant unavailable.")
    return profiles[0]


def set_avatar(
    *,
    concept_id: str | None = None,
    image_concept_id: str | None = None,
    data: bytes | None = None,
    remove: bool = False,
    scope: str = "global_general",
) -> dict:
    actor, concept_id, _ = _participant(concept_id, edit=True)
    from .concept_service import update_concept

    org = get_effective_organisation_concept_id()
    if scope not in SCOPES or (
        scope in ("user_org_default", "organisation_general") and not org
    ):
        raise ValueError("Choose an available avatar scope.")
    previous = [
        r
        for r in _avatar_records([concept_id])
        if (r.get("attributes") or {}).get("avatar_scope") == scope
        and (
            scope not in ("user_org_default", "organisation_general")
            or r["attributes"].get("organisation_concept_id") == org
        )
    ]
    if remove:
        for record in previous:
            update_concept(record["concept_id"], {"attributes.avatar_retired": True})
        return get_profile(concept_id)
    if image_concept_id:
        _, data = load_image(image_concept_id, actor)
    if not data:
        raise ValueError("Choose an image first.")
    inspect_image(data)
    with Image.open(io.BytesIO(data)) as original:
        oriented = ImageOps.exif_transpose(original)
        # Reconstruct pixels so EXIF, comments and private source metadata cannot
        # be carried into the published derivative.
        resized = ImageOps.fit(
            oriented.convert("RGBA"), (256, 256), Image.Resampling.LANCZOS
        )
        clean = Image.new("RGBA", resized.size)
        clean.paste(resized)
        output = io.BytesIO()
        clean.save(output, format="PNG")
    published = output.getvalue()
    sha = hashlib.sha256(published).hexdigest()
    from .blob_uploads import put_bytes_durable

    stored = put_bytes_durable(
        key=f"avatars/{hashlib.sha256(concept_id.encode()).hexdigest()[:24]}/{sha}.png",
        data=published,
        content_type="image/png",
    )
    from .computer_file_copy_service import create_computer_file_copy_instance

    record = create_computer_file_copy_instance(
        user_concept_id=actor,
        organisation_concept_id=org,
        name="Participant avatar.png",
        sha256=sha,
        size_bytes=len(published),
        content_type="image/png",
        blob_backend=stored.ref.backend,
        blob_key=stored.ref.key,
        blob_uri=stored.ref.uri,
        visibility_scope_mode=scope,
        metadata_in_attributes=True,
        maintain_relationship_inverses=False,
        metadata={"avatar_subject_id": concept_id, "avatar_scope": scope},
    )
    persisted = ConceptsRepository.find_one({"concept_id": record.concept_id})
    if not persisted or (persisted.get("attributes") or {}).get("sha256") != sha:
        raise ValueError("The avatar could not be verified after saving.")
    for old in previous:
        update_concept(old["concept_id"], {"attributes.avatar_retired": True})
    return {
        **get_profile(concept_id),
        "saved_scope": scope,
        "saved_avatar_version": sha,
    }


def load_avatar(concept_id: str) -> bytes:
    _participant(concept_id)
    record = _resolve_avatar(_avatar_records([concept_id]), concept_id)
    avatar = (record or {}).get("attributes") or {}
    if not avatar.get("blob_key"):
        raise PermissionError("Avatar unavailable.")
    from .blob_store import get_blob_store_from_env

    data = get_blob_store_from_env().get_bytes(avatar["blob_key"])
    if hashlib.sha256(data).hexdigest() != avatar.get("sha256"):
        raise ValueError("Avatar checksum mismatch.")
    return data


def generate_avatar(*, prompt: str, concept_id: str | None = None) -> dict:
    """Generate a private candidate; publishing is a separate, reusable action."""
    actor, _, _ = _participant(concept_id, edit=True)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        raise ValueError("Describe the image in 1–4000 characters.")
    from ..languagemodels.openai_client import OpenAIClient

    # Bounded single image; no automatic retry that could duplicate provider cost.
    result = (
        OpenAIClient()
        .client.with_options(max_retries=0)
        .images.generate(
            model="gpt-image-1.5",
            prompt=prompt.strip(),
            n=1,
            size="1024x1024",
            quality="low",
            output_format="png",
        )
    )
    if not result.data or not result.data[0].b64_json:
        raise ValueError("The image provider returned no image.")
    data = base64.b64decode(result.data[0].b64_json, validate=True)
    return store_image(
        data=data,
        filename="generated-avatar.png",
        user_concept_id=actor,
        provenance={
            "kind": "generated",
            "model": "gpt-image-1.5",
            "prompt": prompt.strip(),
        },
    )
