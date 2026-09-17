"""Stage snapshot-bound task files without publishing any concepts.

The admitted snapshot supplies the exact content hashes. An existing private
recovery cache may supply bytes, but is never trusted without a fresh digest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .task_project_transfer_service import decoded, verify_snapshot


def stage_project_files(envelope, *, config, origin, blob_store, source_bytes):
    payload = verify_snapshot(envelope, config=config, origin=origin)
    staged = {}
    for document in decoded(payload["body"])["files"]:
        attrs = document["attributes"]
        expected = attrs["sha256"]
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise ValueError("Invalid task source file digest")
        blob_key = f"federated-task-files/{expected}"
        try:
            data = blob_store.get_bytes(blob_key)
        except FileNotFoundError:
            data = source_bytes(document)
        if (
            len(data) != attrs["size_bytes"]
            or hashlib.sha256(data).hexdigest() != expected
        ):
            raise ValueError("Task source bytes differ from the admitted snapshot")
        ref = blob_store.put_bytes(
            blob_key, data, content_type=attrs.get("content_type")
        )
        if hashlib.sha256(blob_store.get_bytes(ref.key)).hexdigest() != expected:
            raise ValueError("Destination task file read-back failed")
        staged[document["concept_id"]] = {
            "blob_backend": ref.backend,
            "blob_key": ref.key,
            "blob_uri": ref.uri,
        }
    return staged


def cached_source_bytes(cache: Path):
    """Only hash-named bytes in the explicitly selected private restore cache."""

    def read(document):
        digest = document["attributes"]["sha256"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid task file digest")
        return (cache / "restored-task-files" / digest).read_bytes()

    return read
