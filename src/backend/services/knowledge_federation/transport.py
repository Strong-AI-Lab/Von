"""Shared operator-configured SSH transport; no caller-selected host or command."""

import json
import shlex
import subprocess

from .continuity import MAX_CONTENT_BYTES
from .protocol import MAX_BYTES, NODE, encode


def remote(config, peer, action, payload=None):
    if action == "content" and peer in config.get("content_endpoints", {}):
        return remote_content_http(config, peer, payload)
    transport = config.get("transports", {})[peer]
    host = transport["ssh_host"]
    if not NODE.fullmatch(host):
        raise ValueError("SSH host must be an admitted config alias")
    command = [
        transport["python"],
        transport["script"],
        "--config",
        transport["config"],
        "--env-file",
        transport["env_file"],
        action,
        "--peer" if action in {"export", "content", "status"} else "--origin",
        config["node_id"],
    ]
    if action == "export" and payload and payload.get("known_digest"):
        command.extend(["--known-digest", payload["known_digest"]])
        payload = None
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            host,
            shlex.join(command),
        ],
        input=encode(payload) if payload is not None else None,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        # Remote stderr can contain runtime diagnostics/connection strings.
        raise RuntimeError(
            f"remote federation {action} failed (exit {result.returncode})"
        )
    if len(result.stdout) > (
        MAX_CONTENT_BYTES if action == "content" else MAX_BYTES + 1024
    ):
        raise ValueError("remote response exceeds pilot bound")
    return json.loads(result.stdout)


def remote_content_http(config, peer, request):
    from urllib.parse import urlparse
    from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

    from .protocol import key, now, peer_config, sign

    endpoint = config["content_endpoints"][peer]
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path != "/content"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("content endpoint must be an admitted loopback SSH tunnel")
    settings = peer_config(config, "imports", peer)
    payload = {
        "schema_version": "von_federation_content_request.v1",
        "origin": config["node_id"],
        "recipient": peer,
        "checked_at": now(),
        "request": request,
    }
    message = Request(
        endpoint,
        data=encode(sign(payload, key(settings))),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError("content transport redirects are not permitted")

    with build_opener(ProxyHandler({}), NoRedirect).open(
        message, timeout=180
    ) as response:
        data = response.read(MAX_CONTENT_BYTES + 1)
    if len(data) > MAX_CONTENT_BYTES:
        raise ValueError("content response exceeds bound")
    return json.loads(data)
