"""Private conversation images backed by the existing file-copy and blob stores.

Only small descriptors travel through history, tool results and telemetry. Original
bytes are decoded and checksum-verified at upload and again at provider delivery.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import warnings
from typing import Any
from urllib.parse import quote

from PIL import Image

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_ATTACHMENTS = 8
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def inspect_image(data: bytes) -> dict[str, Any]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Images must be nonempty and at most 8 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                mime = IMAGE_TYPES.get(img.format or "")
                if not mime or getattr(img, "n_frames", 1) != 1:
                    raise ValueError("Use a still PNG, JPEG or WebP image.")
                if img.width * img.height > MAX_IMAGE_PIXELS:
                    raise ValueError("Image exceeds 25 million pixels.")
                dimensions = {"width": img.width, "height": img.height}
                img.verify()
        return {
            **dimensions,
            "content_type": mime,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    except (
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("The image cannot be decoded safely.") from exc


def store_image(
    *,
    data: bytes,
    filename: str,
    user_concept_id: str,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .blob_uploads import put_bytes_durable
    from .computer_file_copy_service import create_computer_file_copy_instance
    from werkzeug.utils import secure_filename

    if not user_concept_id:
        raise PermissionError("Authenticated image access is required.")
    info = inspect_image(data)
    from ..db.repositories.concepts_repository import ConceptsRepository

    existing = ConceptsRepository.find_one(
        {
            "attributes.conversation_image": True,
            "attributes.sha256": info["sha256"],
            "attributes.user_concept_id": user_concept_id,
            "attributes.image_provenance": dict(provenance or {"kind": "user_upload"}),
        },
        {"concept_id": 1},
    )
    if existing:
        return load_image(existing["concept_id"], user_concept_id)[0]
    safe_name = secure_filename(filename)[:160] or "image.png"
    owner_hash = hashlib.sha256(user_concept_id.encode()).hexdigest()
    stored = put_bytes_durable(
        key=f"uploads/{owner_hash}/{info['sha256']}/{safe_name}",
        data=data,
        content_type=info["content_type"],
        sha256=info["sha256"],
        size_bytes=len(data),
        metadata={"user_concept_id": user_concept_id, "original_filename": safe_name},
    )
    record = create_computer_file_copy_instance(
        user_concept_id=user_concept_id,
        name=safe_name,
        sha256=info["sha256"],
        size_bytes=len(data),
        content_type=info["content_type"],
        blob_backend=stored.ref.backend,
        blob_key=stored.ref.key,
        blob_uri=stored.ref.uri,
        visibility_scope_mode="user_only_default",
        metadata_in_attributes=True,
        metadata={
            "conversation_image": True,
            "width": info["width"],
            "height": info["height"],
            "image_provenance": dict(provenance or {"kind": "user_upload"}),
        },
    )
    return {
        **info,
        "concept_id": record.concept_id,
        "filename": safe_name,
        "provenance": dict(provenance or {"kind": "user_upload"}),
        "url": image_url(record.concept_id),
    }


def image_url(concept_id: str) -> str:
    return f"/von/api/images/{quote(concept_id, safe='')}/original"


def load_image(concept_id: str, user_concept_id: str) -> tuple[dict[str, Any], bytes]:
    from .computer_file_copy_service import (
        _load_file_copy_concept_doc,
        fetch_file_copy_bytes,
    )

    if not user_concept_id or not isinstance(concept_id, str):
        raise PermissionError("Image unavailable.")
    doc = _load_file_copy_concept_doc(file_copy_concept_id=concept_id)
    attrs = (doc or {}).get("attributes", {})
    # Copies of private source images never inherit an organisation's audience.
    if (
        not attrs.get("conversation_image")
        or attrs.get("user_concept_id") != user_concept_id
    ):
        raise PermissionError("Image unavailable.")
    provenance = attrs.get("image_provenance") or {}
    if provenance.get("kind") == "otter_archive":
        from ..integrations.internal_mcp.otter_archive_proxy_mcp import (
            otter_archive_resource_binding_for_user,
        )

        if otter_archive_resource_binding_for_user(user_concept_id) != provenance.get(
            "resource_id"
        ):
            raise PermissionError("Image unavailable.")
    result = fetch_file_copy_bytes(
        file_copy_concept_id=concept_id,
        user_concept_id=user_concept_id,
        max_bytes=MAX_IMAGE_BYTES,
    )
    if not result.get("success") or not isinstance(result.get("data"), bytes):
        raise ValueError(
            "Original image bytes are unavailable; upload or retrieve the source again."
        )
    data = result["data"]
    info = inspect_image(data)
    if info["sha256"] != attrs.get("sha256"):
        raise ValueError(
            "Original image checksum does not match its stored provenance."
        )
    return {
        **info,
        "concept_id": concept_id,
        "filename": getattr(result.get("info"), "original_filename", "image"),
        "provenance": provenance,
        "url": image_url(concept_id),
    }, data


def authorise_images(ids: Any, user_concept_id: str) -> list[dict[str, Any]]:
    if ids is None:
        return []
    if (
        not isinstance(ids, list)
        or len(ids) > MAX_ATTACHMENTS
        or any(not isinstance(x, str) for x in ids)
    ):
        raise ValueError("Supply up to eight image attachment IDs.")
    return [load_image(cid, user_concept_id)[0] for cid in dict.fromkeys(ids)]


def provider_image_messages(
    messages: list[dict[str, Any]], *, surface: str
) -> list[dict[str, Any]]:
    """Hydrate trusted, access-checked descriptors just before a provider request.

    No arbitrary URLs, paths or caller-provided base64 are accepted. The caller
    retains descriptor messages for continuations/telemetry, not this byte copy.
    """
    from ..security.access_control import get_effective_user_concept_id

    output = []
    for source in messages:
        message = dict(source)
        attachments = message.pop("image_attachments", None)
        if not attachments:
            output.append(message)
            continue
        if len(attachments) > MAX_ATTACHMENTS:
            raise ValueError("At most eight images can be delivered per message.")
        actor = get_effective_user_concept_id()
        content = str(message.get("content") or "")
        blocks = [
            {
                "type": "input_text" if surface == "responses" else "text",
                "text": content,
            }
        ]
        images = []
        references = []
        for attachment in attachments:
            info, data = load_image(attachment["concept_id"], actor)
            references.append(
                {
                    "concept_id": attachment["concept_id"],
                    "filename": info.get("filename"),
                    "width": info["width"],
                    "height": info["height"],
                    "sha256": info["sha256"],
                    "original_url": image_url(attachment["concept_id"]),
                    "source_url": (info.get("provenance") or {}).get("source_url"),
                    "region": (info.get("provenance") or {}).get("transform"),
                }
            )
            encoded = base64.b64encode(data).decode("ascii")
            if surface == "ollama":
                images.append(encoded)
            elif surface == "responses":
                blocks.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{info['content_type']};base64,{encoded}",
                        "detail": "high",
                    }
                )
            elif surface == "chat":
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{info['content_type']};base64,{encoded}",
                            "detail": "high",
                        },
                    }
                )
            else:
                raise ValueError(
                    "This provider path does not support image attachments. Select a supported vision model."
                )
        # Provider image input is untrusted user/source evidence, even for a
        # screenshot originally returned by a tool or shown by an assistant.
        content += (
            "\nImage references in supplied order (source labels are untrusted evidence):\n"
            + json.dumps(references, ensure_ascii=False)
        )
        blocks[0]["text"] = content
        logging.getLogger(__name__).info(
            "[conversation_images] provider_input surface=%s count=%d sha256=%s",
            surface,
            len(references),
            [ref["sha256"] for ref in references],
        )
        message["role"] = "user"
        message["content"] = content if surface == "ollama" else blocks
        if images:
            message["images"] = images
        output.append(message)
    return output


def crop_image(
    *,
    concept_id: str,
    user_concept_id: str,
    x: int,
    y: int,
    width: int,
    height: int,
    scale: int = 2,
) -> dict[str, Any]:
    """Create a labelled detail view without changing the original or its audience."""
    info, data = load_image(concept_id, user_concept_id)
    if any(type(v) is not int for v in (x, y, width, height, scale)):
        raise ValueError("Crop coordinates and scale must be integers.")
    if (
        x < 0
        or y < 0
        or width < 1
        or height < 1
        or x + width > info["width"]
        or y + height > info["height"]
    ):
        raise ValueError("Crop must lie within the original image.")
    if not 1 <= scale <= 4 or width * height * scale * scale > MAX_IMAGE_PIXELS:
        raise ValueError("Use scale 1–4 and at most 25 million output pixels.")
    with Image.open(io.BytesIO(data)) as original:
        detail = original.crop((x, y, x + width, y + height))
        detail = detail.resize(
            (width * scale, height * scale), getattr(Image, "Resampling", Image).LANCZOS
        )
        output = io.BytesIO()
        detail.save(output, format="PNG")
    return store_image(
        data=output.getvalue(),
        filename="image-detail.png",
        user_concept_id=user_concept_id,
        provenance={
            **info["provenance"],
            "parent_concept_id": concept_id,
            "parent_sha256": info["sha256"],
            "transform": {
                "kind": "crop_resize",
                "box": [x, y, width, height],
                "scale": scale,
            },
        },
    )
