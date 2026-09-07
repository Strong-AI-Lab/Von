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


def definitions():
    return [
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
        )
    ]
