"""Private coding-run records and immutable activity blobs (JVNAUTOSCI-2751).

Operational records, not task semantics. Only the trusted controller writes;
HTTP exposes actor-bound reads. Task visibility never grants archive access.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import UTC, datetime
from urllib.parse import quote

from ..db.mongo_client import get_db
from ..security.access_control import (
    LEGACY_IDENTITY_HEADER_ACTOR_SOURCE,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id_with_source,
)
from .blob_store import get_blob_store_for_backend_from_env, get_blob_store_from_env
from .organisation_membership_service import resolve_user_organisation_membership

MAX_BATCH_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
SCHEMA = "von_task_run.v1"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(UTC).isoformat()


def collection():
    db = get_db()
    if db is None:
        raise RuntimeError("Task run archive storage unavailable")
    return db["task_run_archives"]


def actor_scope():
    actor, source = get_effective_user_concept_id_with_source()
    org = get_effective_organisation_concept_id()
    if (
        not actor
        or not org
        or source == LEGACY_IDENTITY_HEADER_ACTOR_SOURCE
        or not resolve_user_organisation_membership(actor, org)
    ):
        raise PermissionError("Task run access requires a trusted organisation actor")
    return actor, org


def _run(run_id, *, write=False):
    actor, org = actor_scope()
    row = collection().find_one(
        {"_id": run_id, "organisation_id": org, "audience": actor}
    )
    if not row or (write and row["worker_id"] != actor):
        raise PermissionError("Task run unavailable in this actor scope")
    # Source access is checked again, so revoked conversation access does not
    # leave a permanent second copy accessible via the task.
    if row.get("source_session_id"):
        from .shared_conversation_service import (
            get_accepted_invite_for_user_session,
            resolve_conversation_owner,
        )

        sid = row["source_session_id"]
        if resolve_conversation_owner(session_id=sid) != actor:
            invite = get_accepted_invite_for_user_session(
                user_concept_id=actor, session_id=sid
            )
            if not invite or invite.get("organisation_concept_id") != org:
                raise PermissionError(
                    "Source conversation access is no longer available"
                )
    return row


def register_run(*, task_id, run_id, provenance, source_session_id=None):
    from .task_management_service import get_task

    actor, org = actor_scope()
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Invalid run identity")
    existing = collection().find_one({"_id": run_id})
    if existing:
        row = _run(run_id, write=True)
        if (
            row["task_id"] != task_id
            or row.get("source_session_id") != source_session_id
        ):
            raise ValueError("Run identity already bound to another source")
        return public_record(row)
    task = get_task(task_id)
    if (
        task.get("assignee_concept_id") != actor
        or task.get("organisation_concept_id") != org
    ):
        raise PermissionError("Run must belong to the assigned worker and organisation")
    owner = task.get("created_by_concept_id")
    audience = [actor]
    if owner and resolve_user_organisation_membership(owner, org):
        audience.append(owner)
    if source_session_id:
        from .shared_conversation_service import (
            get_accepted_invite_for_user_session,
            resolve_conversation_owner,
        )

        source_owner = resolve_conversation_owner(session_id=source_session_id)
        audience = [
            user
            for user in audience
            if user == source_owner
            or (
                (
                    get_accepted_invite_for_user_session(
                        user_concept_id=user, session_id=source_session_id
                    )
                    or {}
                ).get("organisation_concept_id")
                == org
            )
        ]
        if actor not in audience:
            raise PermissionError("Worker cannot archive this source conversation")
    row = {
        "_id": run_id,
        "schema_version": SCHEMA,
        "task_id": task_id,
        "worker_id": actor,
        "requester_id": owner,
        "organisation_id": org,
        "audience": audience,
        "source_session_id": source_session_id,
        "provenance": provenance,
        "started_at": provenance.get("started_at") or now(),
        "updated_at": now(),
        "capture_status": "running",
        "cursor": 0,
        "event_count": 0,
        "batches": [],
        "thread_id": None,
    }
    collection().update_one({"_id": run_id}, {"$setOnInsert": row}, upsert=True)
    stored = _run(run_id, write=True)
    if stored["task_id"] != task_id or stored["provenance"] != provenance:
        raise ValueError("Conflicting run registration")
    return public_record(stored)


def public_record(row):
    return {k: v for k, v in row.items() if k not in {"_id", "audience", "batches"}} | {
        "capture_segments": [
            {k: b[k] for k in ("start", "end", "source_sha256")} for b in row["batches"]
        ],
        "run_id": row["_id"],
        "activity_url": run_url(row["task_id"], row["_id"]),
    }


def run_url(task_id, run_id):
    return f"/api/tasks/{quote(task_id, safe='')}/runs/{run_id}"


def list_runs(task_id, *, offset=0, limit=50):
    actor, org = actor_scope()
    rows = (
        collection()
        .find({"task_id": task_id, "organisation_id": org, "audience": actor})
        .sort("started_at", -1)
        .skip(max(0, offset))
        .limit(min(100, max(1, limit)))
    )
    visible = []
    for row in rows:
        try:
            visible.append(public_record(_run(row["_id"])))
        except PermissionError:
            continue
    return {
        "runs": visible,
        "offset": offset,
        "next_offset": offset + len(visible) if len(visible) == limit else None,
    }


def _put(data, key):
    store = get_blob_store_from_env()
    ref = store.put_bytes(key, data, content_type="application/octet-stream")
    receipt = {
        "backend": ref.backend,
        "key": ref.key,
        "sha256": digest(data),
        "bytes": len(data),
    }
    _get(receipt)
    return receipt


def _get(receipt):
    data = get_blob_store_for_backend_from_env(receipt["backend"]).get_bytes(
        receipt["key"]
    )
    if len(data) != receipt["bytes"] or digest(data) != receipt["sha256"]:
        raise RuntimeError("Archive hash/byte read-back mismatch")
    return data


def append_batch(
    run_id, *, start, end, data, source_sha256, event_count, thread_id=None
):
    row = _run(run_id, write=True)
    if not isinstance(data, bytes) or not data or len(data) > MAX_BATCH_BYTES:
        raise ValueError("Invalid activity batch size")
    if end <= start or event_count < 1:
        raise ValueError("Invalid activity cursor")
    old = next((b for b in row["batches"] if b["start"] == start), None)
    if old:
        if (old["end"], old["sha256"], old["source_sha256"], old["event_count"]) != (
            end,
            digest(data),
            source_sha256,
            event_count,
        ):
            raise ValueError("Conflicting activity retry")
        _get(old)
        return public_record(row)
    if row["capture_status"] == "complete" or row["cursor"] != start:
        raise ValueError("Activity cursor is not contiguous")
    receipt = _put(data, f"task-runs/{run_id}/activity/{start}-{digest(data)}")
    batch = receipt | {
        "start": start,
        "end": end,
        "source_sha256": source_sha256,
        "event_count": event_count,
    }
    updated = collection().update_one(
        {"_id": run_id, "cursor": start, "capture_status": {"$ne": "complete"}},
        {
            "$push": {"batches": batch},
            "$set": {
                "cursor": end,
                "updated_at": now(),
                "capture_status": "running",
                "thread_id": thread_id or row["thread_id"],
            },
            "$inc": {"event_count": event_count},
        },
    )
    if not updated.modified_count:
        return append_batch(
            run_id,
            start=start,
            end=end,
            data=data,
            source_sha256=source_sha256,
            event_count=event_count,
            thread_id=thread_id,
        )
    return public_record(_run(run_id))


def capture_failure(run_id, error_type):
    _run(run_id, write=True)
    collection().update_one(
        {"_id": run_id, "capture_status": {"$ne": "complete"}},
        {
            "$set": {
                "capture_status": "pending",
                "capture_error": error_type,
                "updated_at": now(),
            }
        },
    )


def read_activity(run_id, *, task_id=None, cursor=0):
    row = _run(run_id)
    if task_id is not None and row["task_id"] != task_id:
        raise PermissionError("Run does not belong to this task")
    batch = next((b for b in row["batches"] if b["start"] == cursor), None)
    records = []
    if batch:
        for line in _get(batch).decode("utf-8").splitlines():
            value = json.loads(line)
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
            records.append(
                {"text": rendered[:16000], "display_truncated": len(rendered) > 16000}
            )
    return {
        "run": public_record(row),
        "records": records,
        "next_cursor": batch["end"] if batch else cursor,
        "has_more": bool(batch and batch["end"] < row["cursor"]),
        "projection": "Up to 16000 characters per record; download retains full redacted records.",
    }


def finalise_run(run_id, *, archive, manifest):
    from .task_management_service import (
        TaskNotFoundError,
        add_task_attachment,
        get_task,
    )

    row = _run(run_id, write=True)
    if row["capture_status"] == "complete":
        _get(row["archive"])
        return public_record(row)
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("Archive exceeds bounded upload size")
    if (
        manifest.get("run_id") != run_id
        or manifest.get("task_id") != row["task_id"]
        or manifest.get("final_cursor") != row["cursor"]
        or manifest.get("event_count") != row["event_count"]
    ):
        raise ValueError("Final archive does not reconcile captured activity")
    expected_segments = [
        {k: b[k] for k in ("start", "end", "source_sha256")} for b in row["batches"]
    ]
    if manifest.get("source_segments") != expected_segments:
        raise ValueError("Final source hashes differ from captured segments")
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        embedded = json.loads(package.read("manifest.json"))
        if embedded != manifest:
            raise ValueError("Manifest differs from archive")
        names = [entry["name"] for entry in manifest["files"]]
        if len(set(names)) != len(names) or set(package.namelist()) != set(
            names + ["manifest.json"]
        ):
            raise ValueError("Archive file inventory mismatch")
        if sum(item.file_size for item in package.infolist()) > MAX_ARCHIVE_BYTES:
            raise ValueError("Uncompressed archive exceeds bounded read route")
        for entry in manifest["files"]:
            data = package.read(entry["name"])
            if len(data) != entry["bytes"] or digest(data) != entry["sha256"]:
                raise ValueError("Archive file hash/byte mismatch")
        events = package.read("events.jsonl")
        if events != b"".join(_get(batch) for batch in row["batches"]):
            raise ValueError("Archive differs from published activity")
    receipt = _put(archive, f"task-runs/{run_id}/archive/{digest(archive)}.zip")
    url = run_url(row["task_id"], run_id) + "/download"
    # Attachment metadata reveals no source content. Download rechecks the run
    # audience; never use org-wide file-copy visibility for private transcripts.
    try:
        task = get_task(row["task_id"])
    except TaskNotFoundError:
        task = {}
    attachment = {}
    if (
        task.get("assignee_concept_id") == row["worker_id"]
        and task.get("created_by_concept_id") == row["requester_id"]
        and task.get("organisation_concept_id") == row["organisation_id"]
        and task.get("status") != "cancelled"
    ):
        attachment = add_task_attachment(
            row["task_id"],
            filename=f"coding-run-{run_id}.zip",
            uri=url,
            media_type="application/zip",
            size_bytes=len(archive),
            added_by_concept_id=row["worker_id"],
            source={"source_system": "von_task_run", "external_id": run_id},
        )
    collection().update_one(
        {"_id": run_id, "cursor": row["cursor"]},
        {
            "$set": {
                "archive": receipt,
                "archive_url": url,
                "manifest": manifest,
                "attachment_id": attachment.get("attachment_id"),
                "attachment_status": (
                    "linked" if attachment else "task_changed_or_unavailable"
                ),
                "capture_status": "complete",
                "updated_at": now(),
                "finished_at": manifest["finished_at"],
                "capture_error": None,
            }
        },
    )
    return public_record(_run(run_id))


def download_archive(run_id, *, task_id):
    row = _run(run_id)
    if row["task_id"] != task_id or row["capture_status"] != "complete":
        raise PermissionError("Completed archive unavailable")
    return _get(row["archive"])
