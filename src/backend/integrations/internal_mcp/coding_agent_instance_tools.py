"""Discoverable coding-agent instance lifecycle through prepared host bindings."""

from .gateway import MethodDefinition
from .schemas import Schema, make_error_response


def _instances(**kwargs):
    from ...services.coding_agent_instance_service import coding_agent_instances

    try:
        return coding_agent_instances(**kwargs)
    except (
        PermissionError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        RuntimeError,
    ) as exc:
        return make_error_response(
            "coding_agent_instance_unavailable",
            (
                type(exc).__name__ + ": " + str(exc)
                if isinstance(exc, (PermissionError, ValueError))
                else "Instance operation unavailable; inspect operator readiness and private logs."
            ),
        )


def build_coding_agent_instance_tools():
    return [
        MethodDefinition(
            name="coding_agent_android",
            handler=_android,
            input_schema=Schema(
                required={"instance_id": str, "task_id": str},
                optional={
                    "action": str,
                    "run_id": str,
                    "x": int,
                    "y": int,
                    "text": str,
                    "fixture": str,
                },
                allow_unknown=False,
            ),
            output_schema=None,
            category="write",
            description=(
                "Optional native Android test-host capability for a current assigned task. "
                "Discover actual host prerequisites; start/status/stop a disposable run with a stable run_id; "
                "tap/text/open_fixture or attach a screenshot. Uses an operator-enrolled host route, "
                "never creates another coding worker. Emulator evidence is not physical-device coverage."
            ),
        ),
        MethodDefinition(
            name="coding_agent_instances",
            handler=_instances,
            input_schema=Schema(
                required={},
                optional={"action": str, "instance_id": str, "settings": dict},
                allow_unknown=False,
            ),
            output_schema=None,
            category="write",
            description=(
                "List, inspect, provision/reconcile, pause or resume coding-agent instances "
                "for the authenticated delegator in the current organisation. Uses existing "
                "operator-enrolled host/account/identity/credential bindings; cannot create "
                "host accounts or grant new access. Reconcile prepares a paused instance; "
                "resume explicitly enables its enrolled schedule. Settings accepts model "
                "and reasoning_effort; task overrides remain supported. Readiness requires "
                "actual task and recipient evidence, not a selected release or active timer."
            ),
        ),
    ]


def _android(**kwargs):
    from ...services.coding_agent_android_service import coding_agent_android

    try:
        return coding_agent_android(**kwargs)
    except (
        PermissionError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        RuntimeError,
    ) as exc:
        return make_error_response(
            "coding_agent_android_unavailable",
            (
                str(exc)
                if isinstance(exc, (PermissionError, ValueError))
                else "Android operation unavailable; inspect private operator logs."
            ),
        )
