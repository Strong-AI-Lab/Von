"""Operator-only bounded knowledge exchange over an existing authenticated SSH path.

No MCP mutation tool can admit a peer, change subscriptions, or invoke transport.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backend.services.knowledge_federation.protocol import (
    MAX_BYTES,
    encode,
    load_config,
    peer_config,
)
from src.backend.services.knowledge_federation.transport import remote


def exchange(config, peer):
    from src.backend.services.knowledge_federation.service import (
        export_snapshot,
        import_snapshot,
    )

    peer_config(config, "exports", peer)
    peer_config(config, "imports", peer)
    from src.backend.services.knowledge_federation.protocol import compact_snapshot
    from src.backend.services.knowledge_federation.service import REPLICAS, database

    remote_head = remote(config, peer, "status")
    outgoing = compact_snapshot(
        export_snapshot(config, peer), remote_head.get("digest")
    )
    pushed = remote(config, peer, "import", outgoing)
    local_head = database()[REPLICAS].find_one({"_id": peer}, {"digest": 1}) or {}
    incoming = remote(
        config, peer, "export", {"known_digest": local_head.get("digest")}
    )
    pulled = import_snapshot(config, peer, incoming)
    return {
        "peer": peer,
        "push": pushed,
        "pull": pulled,
        "bytes_sent": len(encode(outgoing)),
        "bytes_received": len(encode(incoming)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument(
        "action",
        choices=["export", "import", "exchange", "watch", "content", "serve", "status"],
    )
    parser.add_argument("--peer")
    parser.add_argument("--origin")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--port", type=int, default=5013)
    parser.add_argument("--known-digest")
    args = parser.parse_args()
    from dotenv import load_dotenv

    load_dotenv(args.env_file)
    logging.disable(logging.CRITICAL)
    from src.backend.services.knowledge_federation.service import (
        export_snapshot,
        import_snapshot,
    )

    if args.action == "serve":
        from src.backend.services.knowledge_federation.content_server import serve

        serve(args.config, args.port)
        return 0

    while True:
        config = load_config(args.config)
        try:
            if args.action == "export":
                from src.backend.services.knowledge_federation.protocol import (
                    compact_snapshot,
                )

                result = compact_snapshot(
                    export_snapshot(config, args.peer), args.known_digest
                )
            elif args.action == "status":
                from src.backend.services.knowledge_federation.service import (
                    REPLICAS,
                    database,
                )

                peer_config(config, "imports", args.peer)
                result = (
                    database()[REPLICAS].find_one(
                        {"_id": args.peer}, {"_id": 0, "digest": 1}
                    )
                    or {}
                )
            elif args.action == "content":
                from src.backend.services.knowledge_federation.continuity import (
                    serve_content,
                )
                from src.backend.services.knowledge_federation.service import database

                raw = sys.stdin.buffer.read(8193)
                if len(raw) > 8192:
                    raise ValueError("content request exceeds bound")
                result = serve_content(
                    config, args.peer, json.loads(raw), db=database()
                )
            elif args.action == "import":
                raw = sys.stdin.buffer.read(MAX_BYTES + 1025)
                if len(raw) > MAX_BYTES + 1024:
                    raise ValueError("snapshot exceeds input bound")
                result = import_snapshot(config, args.origin, json.loads(raw))
            else:
                result = exchange(config, args.peer)
            print(json.dumps(result), flush=True)
        except Exception as exc:  # noqa: BLE001
            # Keep polling with typed failures; never log private diagnostics.
            print(
                json.dumps({"status": "failed", "error_type": type(exc).__name__}),
                file=sys.stderr,
                flush=True,
            )
            if args.action != "watch":
                return 1
        if args.action != "watch":
            return 0
        time.sleep(max(15, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
