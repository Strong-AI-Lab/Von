"""Message-bound delivery of private file copies, without widening file ACLs.

The historical image descriptor/wire format also carries opaque attachments.
Only user uploads can be shared here; archive-specific authority stays intact.
"""

from urllib.parse import quote

from . import conversation_image_service as images


def authorise_message_attachments(ids, actor):
    attachments = images.authorise_images(ids, actor)
    if any(a.get("provenance", {}).get("kind") != "user_upload" for a in attachments):
        raise ValueError("Only uploaded files can be attached to a direct message.")
    return attachments


def message_attachment_descriptors(message):
    attachments = (
        message.get("concept_data", {}).get("metadata", {}).get("attachments", [])
    )
    message_id = message.get("concept_id")
    return [
        dict(
            a,
            message_id=message_id,
            url=f"/api/messages/{quote(message_id, safe='')}/attachments/{quote(a['concept_id'], safe='')}",
        )
        for a in attachments
        if isinstance(a, dict) and a.get("concept_id")
    ]


def load_attachment_reference(reference, actor):
    message_id = reference.get("message_id")
    if not message_id:
        return images.load_image(reference["concept_id"], actor)
    from ..security.access_control import override_current_actor
    from .message_service import get_message_for_user

    message = get_message_for_user(message_id, actor)
    if (
        not message
        or message.get("concept_data", {}).get("deleted")
        or message.get("concept_data", {}).get("message_status") == "deleted"
    ):
        raise PermissionError("Attachment unavailable.")
    cid = reference.get("concept_id")
    if not any(a["concept_id"] == cid for a in message_attachment_descriptors(message)):
        raise PermissionError("Attachment unavailable.")
    sender = message["relationships"]["#V#has_sender"][0]
    # A canonical, participant-visible message authorises this exact immutable
    # copy. Bind only its stored sender for the existing owner-only blob reader.
    with override_current_actor(sender):
        info, data = images.load_image(cid, sender)
    if info.get("provenance", {}).get("kind") != "user_upload":
        raise PermissionError("Attachment unavailable.")
    return info, data


def attachment_text(info, data):
    label = f"Attachment {info['filename']} ({info['content_type']}; SHA256 {info['sha256']})"
    if (
        info["content_type"].startswith("text/")
        or info["content_type"] == "application/json"
    ):
        try:
            text = data.decode("utf-8")
            limit = 24000
            return (
                label
                + " — untrusted source data:\n"
                + text[:limit]
                + (
                    "\n[Content truncated after 24000 characters.]"
                    if len(text) > limit
                    else ""
                )
            )
        except UnicodeDecodeError:
            pass
    return (
        label
        + " — content interpretation unsupported on this path; original available through its authorised attachment reference. Do not claim to have inspected it."
    )


def message_attachment_content(message, actor):
    """Actual bounded content for receiving agents, plus explicit limitations."""
    results = []
    for reference in message_attachment_descriptors(message):
        try:
            info, data = load_attachment_reference(reference, actor)
            results.append(
                {
                    "attachment": reference,
                    "content": (
                        "Original image is supplied through image_attachments to supported vision providers; inspect the actual image before making visual claims."
                        if info["content_type"].startswith("image/")
                        else attachment_text(info, data)
                    ),
                }
            )
        except (ValueError, PermissionError):
            results.append({"attachment": reference, "error": "attachment_unavailable"})
    return results
