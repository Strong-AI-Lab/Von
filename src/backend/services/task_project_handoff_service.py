"""Operator-mediated signed handoff: prepare destination, release source, activate.

No timeout activates either side. Retried messages are bound to the same digest
and epoch. Once released, recovery proceeds at the new home; an old snapshot
cannot revoke or replace work it subsequently accepted.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

from pymongo import ReturnDocument

from ..db.mongo_client import get_db
from .knowledge_federation.protocol import encode, key, peer_config, sign
from .task_project_home_service import (
    get_project_home,
    local_node_id,
    transfer_frozen_home,
)
from .task_project_publication_service import RECEIPTS
from .task_project_transfer_service import capture_project, portable

VERSION = "task_project_handoff.v1"


def _verify(envelope, *, config, peer, direction, kind):
    settings = peer_config(config, direction, peer)
    payload = envelope["payload"]
    expected = hmac.new(key(settings), encode(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, envelope.get("signature", "")):
        raise PermissionError("Invalid task home handoff signature")
    if (
        payload.get("schema_version") != VERSION
        or payload.get("kind") != kind
        or payload.get("sender") != peer
        or payload.get("recipient") != config["node_id"]
        or payload.get("project_id") not in settings.get("task_projects", [])
    ):
        raise PermissionError("Task home handoff is not admitted")
    return payload


def publication_receipt(receipt_id, *, config, origin):
    database = get_db()
    receipt = database[RECEIPTS].find_one({"_id": receipt_id})
    if (
        not receipt
        or receipt["origin"] != origin
        or receipt["recipient"] != config["node_id"]
    ):
        raise ValueError("Canonical publication receipt is missing")
    home = get_project_home(receipt["project_id"])
    if (
        not home
        or home["state"] not in {"prepared", "active"}
        or home["snapshot_digest"] != receipt["snapshot_digest"]
    ):
        raise ValueError("Publication home differs from its receipt")
    return sign(
        {
            "schema_version": VERSION,
            "kind": "prepared",
            "sender": config["node_id"],
            "recipient": origin,
            "project_id": receipt["project_id"],
            "snapshot_digest": receipt["snapshot_digest"],
            "source_epoch": receipt["source_epoch"],
            "home_epoch": receipt["home_epoch"],
            "home_url": home["home_url"],
            "source_captured_at": home.get("source_captured_at"),
            "publication_id": receipt_id,
            "canonical_readback": True,
        },
        key(peer_config(config, "imports", origin)),
    )


def release_source(envelope, *, config, destination):
    receipt = _verify(
        envelope, config=config, peer=destination, direction="exports", kind="prepared"
    )
    if local_node_id() != config["node_id"] or not receipt.get("canonical_readback"):
        raise ValueError("Source node or publication read-back is invalid")
    project_id = receipt["project_id"]
    home = get_project_home(project_id)
    expected = receipt["source_epoch"]
    if not home:
        raise ValueError("Source home is absent")
    if home["state"] == "frozen":
        # Reconcile any intervening change rather than trusting an earlier file.
        fresh = capture_project(project_id, config=config, recipient=destination)[
            "payload"
        ]
        if fresh["digest"] != receipt["snapshot_digest"] or home["epoch"] != expected:
            raise ValueError("Source changed after destination preparation")
    moved = transfer_frozen_home(
        project_id=project_id,
        expected_epoch=expected,
        node_id=destination,
        home_url=receipt["home_url"],
        snapshot_digest=receipt["snapshot_digest"],
        receipt=receipt,
    )
    if moved["epoch"] != receipt["home_epoch"]:
        raise ValueError("Destination home epoch mismatch")
    return sign(
        {
            "schema_version": VERSION,
            "kind": "released",
            "sender": config["node_id"],
            "recipient": destination,
            "project_id": project_id,
            "home_epoch": moved["epoch"],
            "snapshot_digest": moved["snapshot_digest"],
            "publication_id": receipt["publication_id"],
            "source_read_only": True,
            "canonical_readback": True,
        },
        key(peer_config(config, "exports", destination)),
    )


def activate_destination(envelope, *, config, origin):
    release = _verify(
        envelope, config=config, peer=origin, direction="imports", kind="released"
    )
    if (
        local_node_id() != config["node_id"]
        or not release.get("source_read_only")
        or not release.get("canonical_readback")
    ):
        raise ValueError("Source release or destination node is invalid")
    database = get_db()
    receipt = database[RECEIPTS].find_one({"_id": release["publication_id"]})
    if not receipt or any(
        receipt.get(k) != release[k]
        for k in ("project_id", "home_epoch", "snapshot_digest")
    ):
        raise ValueError("Source release differs from the canonical publication")
    query = {
        "_id": release["project_id"],
        "state": {"$in": ["prepared", "active"]},
        "epoch": release["home_epoch"],
        "home_node_id": config["node_id"],
        "snapshot_digest": release["snapshot_digest"],
    }
    home = database.task_project_homes.find_one_and_update(
        query,
        {
            "$set": {
                "state": "active",
                "source_release": release,
                "activated_at": datetime.now(UTC),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if home is None:
        raise ValueError("Destination home changed; activation refused")
    return {
        "project_id": release["project_id"],
        "home_epoch": home["epoch"],
        "state": home["state"],
        "snapshot_digest": home["snapshot_digest"],
        "canonical_readback": True,
        "source_release": portable(release),
    }
