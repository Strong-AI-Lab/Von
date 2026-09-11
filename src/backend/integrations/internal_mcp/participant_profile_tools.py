"""Agent parity for the participant profile editor."""

import base64

from .gateway import MethodDefinition
from .schemas import Schema


def participant_profile(**args):
    from ...services import participant_profile_service as service
    from .catalogue import make_error_response

    action = args.pop("action", "get")
    try:
        if action == "get":
            return {
                "success": True,
                "profile": service.get_profile(args.get("concept_id")),
            }
        if action == "generate":
            return {
                "success": True,
                "image": service.generate_avatar(
                    prompt=args.get("prompt"), concept_id=args.get("concept_id")
                ),
            }
        if action in ("set_avatar", "remove_avatar"):
            encoded = args.get("image_base64")
            if encoded and len(encoded) > 12 * 1024 * 1024:
                raise ValueError("Image too large.")
            profile = service.set_avatar(
                concept_id=args.get("concept_id"),
                image_concept_id=args.get("image_concept_id"),
                data=base64.b64decode(encoded, validate=True) if encoded else None,
                remove=action == "remove_avatar",
                scope=args.get("scope", "global_general"),
            )
            return {"success": True, "profile": profile}
        raise ValueError("Unknown participant profile action.")
    except (ValueError, PermissionError) as exc:
        return make_error_response("participant_profile_unavailable", str(exc))


def definitions():
    return [
        MethodDefinition(
            name="participant_profile",
            handler=participant_profile,
            input_schema=Schema(
                required={},
                optional={
                    "action": str,
                    "concept_id": str,
                    "prompt": str,
                    "scope": str,
                    "image_concept_id": str,
                    "image_base64": str,
                },
                allow_unknown=False,
            ),
            output_schema=Schema(required={}, optional={}, allow_unknown=True),
            category="write",
            description="Read a participant profile (action=get), generate a private avatar preview (generate, prompt), publish an avatar (set_avatar with private image_concept_id or PNG/JPEG/WebP image_base64), or remove_avatar. Defaults to the authenticated actor; edits only their own profile. Generation uses one paid image request and does not publish automatically. Scope selects user_only_default, organisation_general, user_org_default (Von combined audience) or global_general. Publishing creates a sanitised 256px derivative with that scope; the private original stays private. Returns canonical profile readback. Use from conversations, concept views or settings.",
        )
    ]
