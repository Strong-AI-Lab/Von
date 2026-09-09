"""Explicit collect-now and receipt tools sharing the existing archive owner binding."""

from .gateway import MethodDefinition
from .schemas import Schema


def call_collection(action, **arguments):
    from ...services import otter_collection_service
    from .otter_archive_proxy_mcp import resolve_otter_archive_invocation_authority

    try:
        resource_id = arguments.pop("resource_id")
        allowed = (
            {"meeting_ids", "created_after", "created_before", "participant_bindings"}
            if action == "enqueue"
            else {"run_id"}
        )
        if set(arguments) - allowed:
            raise ValueError("unexpected_collection_arguments")
        authority = resolve_otter_archive_invocation_authority(resource_id=resource_id)
        result = getattr(otter_collection_service, action)(resource_id, **arguments)
        return {
            **result,
            "authority": {
                **authority.receipt(),
                "access": "collect_private_source"
                if action == "enqueue"
                else "read_only",
            },
        }
    except Exception as exc:  # noqa: BLE001 - redact transport and credential errors
        return {
            "success": False,
            "error_code": getattr(exc, "reason_code", "otter_collection_unavailable"),
            "error_type": type(exc).__name__,
        }


def definitions():
    from functools import partial

    return [
        MethodDefinition(
            name="otter_collect_now",
            handler=partial(call_collection, "enqueue"),
            input_schema=Schema(
                required={"resource_id": str},
                optional={
                    "meeting_ids": list,
                    "created_after": str,
                    "created_before": str,
                    "participant_bindings": dict,
                },
                allow_unknown=False,
            ),
            output_schema=Schema(required={}, optional={}, allow_unknown=True),
            category="write",
            ordinary_turn_effect=True,
            ordinary_turn_trusted_argument_bindings={
                "resource_id": "otter_archive_resource_id"
            },
            description="Collect new Otter meetings now or refresh exact IDs/URLs into the private archive and link participants. Dates use YYYY-MM-DD. Optional participant_bindings maps requested meeting ID to exact source speaker label to reviewed existing person concept ID. Returns a queued run ID; inspect otter_collection_status before claiming collection complete.",
        ),
        MethodDefinition(
            name="otter_collection_status",
            handler=partial(call_collection, "status"),
            input_schema=Schema(
                required={"resource_id": str},
                optional={"run_id": str},
                allow_unknown=False,
            ),
            output_schema=Schema(required={}, optional={}, allow_unknown=True),
            category="read",
            ordinary_turn_trusted_argument_bindings={
                "resource_id": "otter_archive_resource_id"
            },
            description="Inspect Otter collection runs, failures, participant concept links and scheduled collection configuration. Queued or running is not imported.",
        ),
    ]
