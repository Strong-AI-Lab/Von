"""Principal-bound local MCP transport for an operator-installed coding agent.

Reuses the canonical catalogue, gateway, schemas and ACLs. The owner-only binding
is installed outside Git; neither tool arguments nor the model select an actor,
organisation, database, executable or credential. This is a trusted local SAIL
operator surface, not isolation from another process running as the same Unix user.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path


def load_binding(path):
    path = Path(path).resolve(strict=True)
    mode = path.stat()
    if mode.st_uid != os.getuid() or mode.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError(
            "MCP binding must be owned by the operator and not writable by others"
        )
    value = json.loads(path.read_text())
    for key in ("actor_id", "organisation_id"):
        if not isinstance(value.get(key), str) or not value[key].startswith("#V#"):
            raise ValueError("Missing installed " + key)
    if not isinstance(value.get("methods"), list) or not value["methods"]:
        raise ValueError("Explicit canonical method list required")
    return value


class BoundTools:
    def __init__(self, binding, catalogue, gateway):
        self.binding, self.catalogue, self.gateway = binding, catalogue, gateway
        self.names = tuple(sorted(set(binding["methods"])))
        for name in self.names:
            catalogue.get(name)  # fail installation on an unavailable capability

    @contextmanager
    def actor(self):
        from src.backend.integrations.internal_mcp.gateway import (
            INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
            bind_internal_mcp_actor_context_source,
        )
        from src.backend.security.access_control import (
            force_access_control_enforcement,
            override_current_actor,
        )

        actor, org = self.binding["actor_id"], self.binding["organisation_id"]
        with (
            override_current_actor(actor, org),
            force_access_control_enforcement(),
            bind_internal_mcp_actor_context_source(
                INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
                preexisting_actor_context=(actor, org),
            ),
        ):
            # Membership is checked on every call, not merely at installation.
            from src.backend.services.message_service import (
                authorise_direct_message_participants,
            )

            allowed, _ = authorise_direct_message_participants(
                sender_id=actor, recipient_ids=[actor], organisation_concept_id=org
            )
            if not allowed:
                raise PermissionError(
                    "Installed principal is not a current organisation member"
                )
            yield

    def call(self, name, arguments):
        if name != "von_context" and name not in self.names:
            raise PermissionError("Method is not installed for this principal")
        arguments = dict(arguments or {})
        for field in ("acting_user_concept_id", "actor_concept_id", "sender_id"):
            if arguments.get(field) not in (None, "", self.binding["actor_id"]):
                return {"success": False, "error_code": "operator_actor_mismatch"}
        with self.actor():
            if name == "von_context":
                return {
                    "success": True,
                    "actor_id": self.binding["actor_id"],
                    "organisation_id": self.binding["organisation_id"],
                    "identity_source": "operator_owned_local_binding",
                    "methods": list(self.names),
                }
            # The existing gateway checks schemas, actor scope and governed effects.
            # Authority arguments cannot replace the context bound above.
            return self.gateway.invoke(name, dict(arguments or {})).payload


async def serve(bound):
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    from src.backend.integrations.internal_mcp.schemas import schema_to_json_schema

    app = Server("von-coding-operator")

    @app.list_tools()
    async def list_tools():
        return [
            Tool(
                name="von_context",
                description="Verify installed Von actor and organisation before using canonical tools.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            *[
                Tool(
                    name=n,
                    description=bound.catalogue.get(n).description or n,
                    inputSchema=schema_to_json_schema(
                        bound.catalogue.get(n).input_schema
                    ),
                )
                for n in bound.names
            ],
        ]

    @app.call_tool()
    async def call_tool(name, arguments):
        try:
            result = await asyncio.to_thread(bound.call, name, arguments)
        except Exception as exc:  # noqa: BLE001 - redact all backend exceptions at the transport boundary
            # Raw backend exceptions can contain connection strings. Private logs
            # and canonical typed errors retain diagnosis without leaking secrets.
            result = {
                "success": False,
                "error_code": "operator_tool_failed",
                "error_type": type(exc).__name__,
            }
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", required=True)
    args = parser.parse_args()
    binding = load_binding(args.binding)
    # Installed release and environment paths, never tool-payload paths.
    root = Path(binding["backend_root"]).resolve(strict=True)
    sys.path.insert(0, str(root))
    os.chdir(root)
    from dotenv import dotenv_values

    settings = dotenv_values(binding["environment_file"])
    for key in binding.get("environment_keys", ["MONGO_URI", "VON_DB_NAME"]):
        if settings.get(key) is not None:
            os.environ[key] = settings[key]
    os.environ.update(binding.get("environment", {}))
    from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    catalogue = build_default_catalogue()
    gateway = InternalMCPGateway(
        catalogue=catalogue, transport=InternalMCPTransport(), enabled=True
    )
    asyncio.run(serve(BoundTools(binding, catalogue, gateway)))


if __name__ == "__main__":
    main()
