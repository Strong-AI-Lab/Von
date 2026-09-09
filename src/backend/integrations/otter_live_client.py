"""OAuth-authenticated read client for Otter's official hosted MCP service.

Credentials belong to the collector, never to the archive query subprocess.
No interactive login is attempted by an unattended collection.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

OTTER_MCP_URL = "https://mcp.otter.ai/mcp"


def private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, filename = tempfile.mkstemp(dir=path.parent, prefix=".private-")
    temporary = Path(filename)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    path.chmod(0o600)


class CredentialStore:
    def __init__(self, root: Path):
        self.root = root

    def read(self, name, model):
        path = self.root / (name + ".json")
        return model.model_validate_json(path.read_text()) if path.exists() else None

    async def get_tokens(self):
        token = self.read("tokens", OAuthToken)
        if token and token.expires_in is not None:
            # Legacy files use their write time; a restart must not renew a token.
            path = self.root / "tokens.json"
            saved = json.loads(path.read_text())
            expiry = saved.get("expires_at", path.stat().st_mtime + token.expires_in)
            token.expires_in = max(0, int(expiry - time.time()))
        return token

    async def set_tokens(self, tokens):
        data = tokens.model_dump(mode="json")
        if tokens.expires_in is not None:
            data["expires_at"] = time.time() + tokens.expires_in
        private_json(self.root / "tokens.json", data)

    async def get_client_info(self):
        return self.read("client", OAuthClientInformationFull)

    async def set_client_info(self, client_info):
        private_json(self.root / "client.json", client_info.model_dump(mode="json"))


def unwrap(value):
    """Decode nested MCP text envelopes without altering source text."""
    for _ in range(8):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict) and value.get("isError"):
            raise RuntimeError("otter_tool_failed")
        if isinstance(value, dict) and value.get("structuredContent") is not None:
            value = value["structuredContent"]
        elif isinstance(value, dict) and isinstance(value.get("content"), list):
            texts = [x["text"] for x in value["content"] if x.get("type") == "text"]
            if len(texts) != 1:
                raise ValueError("otter_response_shape_unsupported")
            value = texts[0]
        elif isinstance(value, dict) and set(value) == {"result"}:
            value = value["result"]
        elif isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return value
        else:
            return value
    raise ValueError("otter_response_nesting_exceeded")


class PersistentOAuthProvider(OAuthClientProvider):
    async def _initialize(self):
        await super()._initialize()
        token = self.context.current_tokens
        if token and token.expires_in is not None:
            self.context.token_expiry_time = time.time() + token.expires_in - 30
        # SDK 1.x does not persist discovery or initialise expiry from stored tokens.
        # Rediscover from Otter's fixed official issuer before unattended refresh.
        if token and token.refresh_token:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://otter.ai/.well-known/oauth-authorization-server"
                )
                response.raise_for_status()
                self.context.oauth_metadata = OAuthMetadata.model_validate(
                    response.json()
                )
                self.context.auth_server_url = "https://otter.ai/"


@asynccontextmanager
async def otter_session(
    credential_root: Path, *, redirect_handler=None, callback_handler=None
):
    async def no_login(_url):
        raise RuntimeError("otter_reauthorisation_required")

    provider = PersistentOAuthProvider(
        server_url=OTTER_MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Von private Otter collector",
            redirect_uris=["http://127.0.0.1:18764/callback"],
            token_endpoint_auth_method="none",
        ),
        storage=CredentialStore(credential_root),
        redirect_handler=redirect_handler or no_login,
        callback_handler=callback_handler,
    )
    async with (
        streamablehttp_client(OTTER_MCP_URL, auth=provider) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def authorise(credential_root: Path):
    """Local operator login with a loopback-only OAuth callback; SDK checks state."""
    from urllib.parse import parse_qs, urlparse

    future = asyncio.get_running_loop().create_future()

    async def receive(reader, writer):
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            target = header.split(b" ", 2)[1].decode("ascii")
            parsed = urlparse(target)
            query = parse_qs(parsed.query)
            if parsed.path != "/callback" or "code" not in query:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            else:
                body = b"Otter authorisation received. You may close this tab."
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
            await writer.drain()
            if parsed.path == "/callback" and "code" in query and not future.done():
                future.set_result((query["code"][0], query.get("state", [None])[0]))
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def redirect(url):
        private_json(credential_root / "authorisation-request.json", {"url": url})
        print(
            "Otter authorisation ready in private authorisation-request.json",
            flush=True,
        )

    async def callback():
        return await future

    server = await asyncio.start_server(receive, "127.0.0.1", 18764)
    try:
        async with (
            server,
            otter_session(
                credential_root, redirect_handler=redirect, callback_handler=callback
            ) as session,
        ):
            listing = await session.list_tools()
            private_json(
                credential_root / "tool-schemas.json",
                [t.model_dump(mode="json") for t in listing.tools],
            )
            return {"authenticated": True, "tools": [t.name for t in listing.tools]}
    finally:
        (credential_root / "authorisation-request.json").unlink(missing_ok=True)
