"""Reusable detail recovery for conversation images from any supported source."""

from .gateway import MethodDefinition
from .schemas import Schema


def crop_conversation_image(**kwargs):
    from ...security.access_control import get_effective_user_concept_id
    from ...services.conversation_image_service import crop_image
    from .catalogue import make_error_response

    try:
        image = crop_image(user_concept_id=get_effective_user_concept_id(), **kwargs)
        return {
            "success": True,
            "image_attachments": [image],
            "note": "Derived detail view. Resizing does not restore information absent from the original.",
        }
    except (ValueError, PermissionError) as exc:
        return make_error_response("image_detail_unavailable", str(exc))


def read_presentation_slides(**kwargs):
    from ...security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id,
    )
    from ...services.presentation_evidence_service import (
        read_presentation_slides as read,
    )
    from .catalogue import make_error_response

    try:
        return read(
            user_concept_id=get_effective_user_concept_id(),
            organisation_concept_id=get_effective_organisation_concept_id(),
            **kwargs,
        )
    except (ValueError, PermissionError) as exc:
        return make_error_response("presentation_evidence_unavailable", str(exc))


def definitions():
    return [
        MethodDefinition(
            name="read_presentation_slides",
            handler=read_presentation_slides,
            input_schema=Schema(
                required={"file_copy_concept_id": str},
                optional={"offset": int, "limit": int, "include_ocr": bool},
                allow_unknown=False,
            ),
            output_schema=Schema(required={}, optional={}, allow_unknown=True),
            category="read",
            description="Read a private PPTX or PDF file copy as numbered slide images plus native text and speaker notes. Returns actual images to the vision model, source hashes, rendering provenance and pagination (offset default 0, limit 1–4 default 4). Optional include_ocr returns cached OCR tied to image hash and engine version. Detects PPTX bytes even with incorrect .bin metadata. Use for slide diagrams, equations, tables and research presentations; cite slide numbers and image URLs, distinguish visible evidence from inference. Does not send mail or publish source content.",
        ),
        MethodDefinition(
            name="crop_conversation_image",
            handler=crop_conversation_image,
            input_schema=Schema(
                required={
                    "concept_id": str,
                    "x": int,
                    "y": int,
                    "width": int,
                    "height": int,
                },
                optional={"scale": int},
                allow_unknown=False,
            ),
            output_schema=Schema(required={}, optional={}, allow_unknown=True),
            category="read",
            description="Inspect a crop of a conversation image, preserving its source and private audience. Supply pixel x/y/width/height and optional integer scale 1–4 (default 2). Use the resulting actual image to recover small text or diagram detail; report what remains unreadable. Original bytes remain unchanged.",
        ),
    ]
