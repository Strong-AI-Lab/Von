"""Operate a bounded task-project transfer using canonical federation services.

Run in the selected source or destination environment. Config/evidence/snapshot
files are private operator inputs. Commands never enable a worker or invoke a
model. See docs/engineering/task_project_home_transfer.md for ordering/recovery.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=[
            "register",
            "freeze",
            "capture",
            "preflight",
            "stage-files",
            "publish",
            "receipt",
            "release",
            "activate",
        ],
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--peer", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--organisation")
    parser.add_argument("--project")
    parser.add_argument("--input")
    parser.add_argument("--preflight")
    parser.add_argument("--staged-files")
    parser.add_argument("--cache")
    parser.add_argument("--blob-root")
    parser.add_argument("--epoch", type=int)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    from src.backend.services.knowledge_federation.protocol import (
        load_config,
        private_file,
        peer_config,
    )
    from src.backend.security.access_control import (
        override_current_actor,
        force_access_control_enforcement,
    )
    from src.backend.services.task_project_transfer_service import (
        capture_project,
        verify_snapshot,
        portable,
        decoded,
    )
    from src.backend.services import task_project_home_service as homes
    from src.backend.services import task_project_publication_service as publication
    from src.backend.services import task_project_handoff_service as handoff
    from src.backend.services.task_project_file_transfer_service import (
        stage_project_files,
        cached_source_bytes,
    )
    from src.backend.services.blob_store import LocalBlobStore
    from src.backend.db.mongo_client import get_db
    from src.backend.services.task_project_service import get_task_project

    def read(path):
        if not path:
            raise ValueError("Required private input file is missing")
        return json.loads(private_file(str(Path(path).resolve())).read_text())

    config = load_config(args.config)
    if homes.local_node_id() != config["node_id"]:
        raise ValueError("VON_TASK_HOME_NODE_ID must identify the selected environment")
    with (
        override_current_actor(args.actor, args.organisation),
        force_access_control_enforcement(),
    ):
        operation = args.operation
        if operation in {"register", "freeze"}:
            settings = peer_config(config, "exports", args.peer)
            if args.project not in settings.get("task_projects", []):
                raise PermissionError("Project not admitted for transfer")
            project = get_task_project(args.project)
            evidence = read(args.input)
            if operation == "register":
                result = homes.register_project_home(
                    project_id=args.project,
                    node_id=config["node_id"],
                    home_url=config["home_url"],
                    source_identity={
                        k: project[k] for k in ("source_site", "source_id")
                    },
                    evidence=evidence,
                )
            else:
                result = homes.freeze_project_home(
                    project_id=args.project,
                    expected_epoch=args.epoch,
                    evidence=evidence,
                )
        elif operation == "capture":
            result = capture_project(args.project, config=config, recipient=args.peer)
        elif operation in {"preflight", "stage-files", "publish"}:
            envelope = read(args.input)
            payload = verify_snapshot(envelope, config=config, origin=args.peer)
            if payload["actor"] != args.actor:
                raise PermissionError("Snapshot actor differs from operator scope")
            if operation == "preflight":
                body = decoded(payload["body"])
                documents = body["concepts"] + body["files"]
                ids = [r["concept_id"] for r in documents]
                database = get_db()
                existing = {
                    r["concept_id"]: r
                    for r in database.concepts.find({"concept_id": {"$in": ids}})
                }
                for document in documents:
                    if document["concept_id"] in existing:
                        publication.check_identity_and_retention(
                            existing[document["concept_id"]], document
                        )
                result = {
                    "snapshot_digest": payload["digest"],
                    "expected_destination_digests": {
                        cid: publication.concept_digest(existing.get(cid))
                        for cid in ids
                    },
                    "expected_destination_text_digest": publication.destination_text_digest(
                        database, ids
                    ),
                }
            else:
                if not args.blob_root:
                    raise ValueError("Explicit destination blob root required")
                store = LocalBlobStore(Path(args.blob_root))
                if operation == "stage-files":
                    if not args.cache:
                        raise ValueError(
                            "Explicit private source recovery cache required"
                        )
                    result = stage_project_files(
                        envelope,
                        config=config,
                        origin=args.peer,
                        blob_store=store,
                        source_bytes=cached_source_bytes(Path(args.cache)),
                    )
                else:
                    preflight = read(args.preflight)
                    if preflight.pop("snapshot_digest") != payload["digest"]:
                        raise ValueError("Preflight belongs to another snapshot")
                    result = publication.publish_project_snapshot(
                        envelope,
                        config=config,
                        origin=args.peer,
                        staged_files=read(args.staged_files),
                        blob_store=store,
                        **preflight,
                    )
        elif operation == "receipt":
            result = handoff.publication_receipt(
                read(args.input)["_id"], config=config, origin=args.peer
            )
        elif operation == "release":
            result = handoff.release_source(
                read(args.input), config=config, destination=args.peer
            )
        else:
            result = handoff.activate_destination(
                read(args.input), config=config, origin=args.peer
            )
    output = Path(args.output).resolve()
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(portable(result), separators=(",", ":")))
    temporary.chmod(0o600)
    temporary.replace(output)
    print(
        json.dumps(
            {"operation": args.operation, "output": str(output), "completed": True}
        )
    )


if __name__ == "__main__":
    main()
