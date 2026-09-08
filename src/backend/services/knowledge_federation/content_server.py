"""Optional loopback-only content reader for peers without reverse SSH access.

Reach it through an operator-managed SSH reverse tunnel. Both the request and
response are pairwise authenticated. This surface cannot change subscriptions,
import records, write files, or run tasks.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .continuity import serve_content
from .protocol import encode, key, load_config, peer_config


def handle_request(config, envelope, *, db):
    payload = envelope.get("payload", {})
    peer = payload.get("origin")
    settings = peer_config(config, "exports", peer)
    expected = hmac.new(key(settings), encode(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(envelope.get("signature", ""))):
        raise PermissionError("content request authentication failed")
    if (
        payload.get("schema_version") != "von_federation_content_request.v1"
        or payload.get("recipient") != config["node_id"]
        or abs(
            (
                datetime.now(UTC) - datetime.fromisoformat(payload["checked_at"])
            ).total_seconds()
        )
        > 300
    ):
        raise PermissionError("content request binding/freshness mismatch")
    return serve_content(config, peer, payload["request"], db=db)


def serve(config_path, port):
    from .service import database

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No request content/identifiers in service logs.

        def do_POST(self):
            self.connection.settimeout(180)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.path != "/content" or not 0 < size <= 16384:
                    raise ValueError("invalid bounded content request")
                body = encode(
                    handle_request(
                        load_config(config_path),
                        json.loads(self.rfile.read(size)),
                        db=database(),
                    )
                )
                self.send_response(200)
            except Exception as exc:  # noqa: BLE001
                body = encode(
                    {"error": "content_unavailable", "error_type": type(exc).__name__}
                )
                self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
