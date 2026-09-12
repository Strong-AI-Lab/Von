"""Decode and retain ordered conversational output using the existing image store.

Only recognised visible provider/MCP forms enter this path. It never fetches an
arbitrary URL, interprets private reasoning, or retries a generation request.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from typing import Any

from ..languagemodels.structured_tool_calling.types import LLMContentPart
from . import conversation_image_service as images


class VisibleTextProjection(str):
    """Legacy string projection with retained rich parts for capable consumers.

    Ordinary chat uses LLMResponse instead. Legacy call sites receive a useful
    text/caption and can read content_parts (also copied to response metadata).
    """

    def __new__(cls, text: str, content_parts: list[dict[str, Any]]):
        instance = super().__new__(cls, text)
        instance.content_parts = content_parts
        return instance


def decode_image(encoded: Any, mime_type: str | None = None) -> bytes:
    if (
        not isinstance(encoded, str)
        or len(encoded) > ((images.MAX_IMAGE_BYTES + 2) // 3) * 4
    ):
        raise ValueError("Image output exceeds the supported encoded size.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Image output is not valid base64.") from exc
    info = images.inspect_image(data)
    if mime_type and mime_type != info["content_type"]:
        raise ValueError("Image output does not match its declared media type.")
    return data


def openai_visible_parts(response: Mapping[str, Any]) -> list[LLMContentPart]:
    parts = []
    for index, item in enumerate(response.get("output") or []):
        if not isinstance(item, Mapping):
            continue
        part_id = str(
            item.get("id") or f"{response.get('id') or 'response'}-output-{index}"
        )
        if item.get("type") == "message":
            for offset, content in enumerate(item.get("content") or []):
                if (
                    isinstance(content, Mapping)
                    and content.get("type") == "output_text"
                    and isinstance(content.get("text"), str)
                ):
                    parts.append(
                        LLMContentPart(
                            "text", f"{part_id}-{offset}", text=content["text"]
                        )
                    )
        elif item.get("type") == "image_generation_call":
            provenance = {
                "kind": "generated",
                "provider": "openai",
                "response_id": response.get("id"),
                "item_id": item.get("id"),
                "model": response.get("model"),
                "image_model": item.get("model"),
                "generation_status": item.get("status"),
            }
            if item.get("action") in {"edit", "generate"}:
                provenance["action"] = item["action"]
            # Revised prompts are private generation provenance, never an automatic caption.
            if isinstance(item.get("revised_prompt"), str):
                provenance["revised_prompt"] = item["revised_prompt"]
            if item.get("status") != "completed":
                parts.append(
                    LLMContentPart(
                        "media_error",
                        part_id,
                        text="Image generation did not complete.",
                        provenance=provenance,
                    )
                )
                continue
            try:
                mime = {
                    "png": "image/png",
                    "jpeg": "image/jpeg",
                    "webp": "image/webp",
                }.get(item.get("output_format"))
                if item.get("output_format") and mime is None:
                    raise ValueError("Unsupported image output format.")
                data = decode_image(item.get("result"), mime)
                parts.append(
                    LLMContentPart(
                        "image",
                        part_id,
                        image_data=data,
                        mime_type=images.inspect_image(data)["content_type"],
                        provenance=provenance,
                    )
                )
            except ValueError as exc:
                parts.append(
                    LLMContentPart(
                        "media_error",
                        part_id,
                        text=f"The provider returned an image that could not be retained: {exc}",
                        provenance=provenance,
                    )
                )
    return parts


def without_image_bytes(value: Any) -> Any:
    """Keep the private provider trace useful without retaining base64 duplicates."""
    if isinstance(value, Mapping):
        media = value.get("type") in {
            "image_generation_call",
            "image",
            "input_image",
            "image_url",
        }
        return {
            key: (
                "[image bytes omitted]"
                if media and key in {"result", "data", "image_url"}
                else without_image_bytes(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [without_image_bytes(item) for item in value]
    return value


def public_asset(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """No private prompts, blob paths or provider state in display payloads."""
    value = {
        key: descriptor[key]
        for key in (
            "concept_id",
            "content_type",
            "width",
            "height",
            "sha256",
            "size_bytes",
            "filename",
            "url",
        )
        if key in descriptor
    }
    provenance = descriptor.get("provenance") or {}
    value["provenance"] = {
        key: provenance[key]
        for key in (
            "kind",
            "parent_concept_ids",
            "parent_concept_id",
            "input_concept_ids",
            "artifact_id",
            "source_url",
        )
        if key in provenance
    }
    return value


def retain_parts(
    parts: Sequence[LLMContentPart],
    *,
    actor: str | None,
    turn_id: str | None,
    parent_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    retained = []
    for part in parts:
        entry: dict[str, Any] = {
            "part_id": part.part_id,
            "kind": part.kind,
            "version": 1,
        }
        if part.kind != "image":
            entry["text"] = part.text
            if part.kind == "media_error":
                entry["generation_status"] = part.provenance.get(
                    "generation_status", "failed"
                )
        else:
            try:
                info = images.inspect_image(part.image_data or b"")
                ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[
                    info["content_type"]
                ]
                descriptor = images.store_image(
                    data=part.image_data or b"",
                    filename=f"conversation-{info['sha256'][:16]}.{ext}",
                    user_concept_id=actor,
                    provenance={
                        **part.provenance,
                        "turn_id": turn_id,
                        "input_concept_ids": list(dict.fromkeys(parent_ids)),
                        **(
                            {"parent_concept_ids": list(parent_ids)}
                            if part.provenance.get("action") == "edit"
                            and len(parent_ids) == 1
                            else {}
                        ),
                    },
                )
                entry["asset"] = public_asset(descriptor)
            except Exception:  # noqa: BLE001 - delivery errors must not repeat paid generation
                # A generation has already occurred. A storage/UI failure must
                # never turn into an automatic second paid provider request.
                entry.update(
                    kind="media_error",
                    text="An image was returned, but it could not be stored. Image generation has not been retried.",
                    error_code="image_storage_failed",
                    generation_status=part.provenance.get(
                        "generation_status", "completed"
                    ),
                    recovery={
                        key: part.provenance[key]
                        for key in (
                            "provider",
                            "response_id",
                            "item_id",
                            "tool_name",
                            "call_id",
                        )
                        if part.provenance.get(key)
                    },
                )
        retained.append(entry)
    return retained


def image_assets(parts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    assets = {}
    for part in parts:
        asset = part.get("asset")
        if (
            part.get("kind") == "image"
            and isinstance(asset, Mapping)
            and asset.get("concept_id")
        ):
            assets[asset["concept_id"]] = dict(asset)
    return list(assets.values())


def retain_tool_images(
    payload: Any, *, actor: str | None, turn_id: str, tool_name: str, call_id: str
) -> Any:
    """MCP ImageContent and embedded BlobResourceContents share the same store.

    Resource links remain references; this is deliberately not a URL fetcher.
    Existing tools' actor-checked image_attachments continue to work unchanged.
    """
    if not isinstance(payload, Mapping) or not isinstance(payload.get("content"), list):
        return payload
    parts = []
    cleaned = []
    for index, item in enumerate(payload["content"]):
        if not isinstance(item, Mapping):
            cleaned.append(item)
            continue
        resource = item.get("resource") if item.get("type") == "resource" else None
        resource = resource if isinstance(resource, Mapping) else {}
        encoded = (
            item.get("data") if item.get("type") == "image" else resource.get("blob")
        )
        mime = item.get("mimeType") or resource.get("mimeType")
        if item.get("type") == "text":
            parts.append(
                LLMContentPart(
                    "text", f"{call_id}-{index}", text=str(item.get("text") or "")
                )
            )
            cleaned.append(dict(item))
            continue
        if item.get("type") != "image" and not (
            encoded is not None and str(mime or "").startswith("image/")
        ):
            cleaned.append(dict(item))
            continue
        try:
            if mime not in images.IMAGE_TYPES.values():
                raise ValueError("Unsupported image output media type.")
            parts.append(
                LLMContentPart(
                    "image",
                    f"{call_id}-{index}",
                    image_data=decode_image(encoded, mime),
                    mime_type=mime,
                    provenance={
                        "kind": "tool_output",
                        "tool_name": tool_name,
                        "call_id": call_id,
                    },
                )
            )
        except ValueError as exc:
            parts.append(
                LLMContentPart("media_error", f"{call_id}-{index}", text=str(exc))
            )
    if not any(part.kind != "text" for part in parts):
        return payload
    retained = retain_parts(parts, actor=actor, turn_id=turn_id)
    return {
        **payload,
        "content": cleaned,
        "content_parts": retained,
        "image_attachments": image_assets(retained),
    }
