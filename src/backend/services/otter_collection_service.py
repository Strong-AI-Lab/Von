"""Owner-bound live collection, with a private durable queue and resumable receipts.

The host scheduler and Von tools run the same worker. SQLite here is operational
state only; all represented concepts/assertions use canonical Von services.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ..integrations.internal_mcp.otter_archive_proxy_mcp import (
    _build_otter_archive_config,
)
from ..integrations.otter_live_client import otter_session, unwrap


def now():
    return datetime.now(UTC).isoformat()


def config(resource_id):
    archive = _build_otter_archive_config(resource_id=resource_id)
    root = archive.database.parent / "collection"
    settings_path = root / "settings.json"
    if not settings_path.exists():
        raise RuntimeError("otter_collection_not_configured")
    settings = json.loads(settings_path.read_text())
    if settings.get("owner_user_concept_id") != archive.owner_user_concept_id:
        raise PermissionError("otter_collection_owner_mismatch")
    if not settings.get("enabled"):
        raise RuntimeError("otter_collection_disabled")
    return archive, root, settings


def source_accounts(archive, settings):
    """Trusted operator configuration; tool callers cannot select credentials."""
    accounts = [
        {
            "id": "primary",
            "email": settings["account_email"],
            "credentials": archive.database.parent / "credentials",
        }
    ]
    for item in settings.get("additional_accounts", []):
        key = item["id"]
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", key) or key == "primary":
            raise ValueError("invalid_otter_source_account")
        if any(
            x["id"] == key or x["email"].casefold() == item["email"].casefold()
            for x in accounts
        ):
            raise ValueError("duplicate_otter_source_account")
        accounts.append(
            {
                "id": key,
                "email": item["email"],
                "credentials": archive.database.parent
                / "accounts"
                / key
                / "credentials",
            }
        )
    return accounts


@asynccontextmanager
async def connected_sources(accounts, receipt):
    async with AsyncExitStack() as stack:
        sources = []
        receipt["source_accounts"] = []
        for account in accounts:
            state = {"id": account["id"], "email": account["email"], "connected": False}
            receipt["source_accounts"].append(state)
            try:
                session = await stack.enter_async_context(
                    otter_session(account["credentials"])
                )
                info = unwrap(await session.call_tool("otter_get_user_info", {}))
                if not isinstance(info, str) or not re.search(
                    r"^Email:\s*" + re.escape(account["email"]) + r"\s*$",
                    info,
                    re.MULTILINE | re.IGNORECASE,
                ):
                    raise PermissionError("otter_source_account_mismatch")
                name = re.search(r"^Name:\s*(.+)$", info, re.MULTILINE)
                state["connected"] = True
                sources.append(
                    {**account, "session": session, "name": name[1] if name else None}
                )
            except Exception as exc:  # noqa: BLE001 - record account failure without source secrets
                state["error_type"] = type(exc).__name__
                receipt["discovery_complete"] = False
        if not sources:
            raise RuntimeError("otter_no_source_accounts_connected")
        yield sources


async def discover_sources(sources, arguments, receipt):
    ids = []
    for source in sources:
        state = next(x for x in receipt["source_accounts"] if x["id"] == source["id"])
        cursor = None
        seen = set()
        complete = False
        try:
            for _ in range(100):
                query = (
                    {"cursor": cursor, "page_size": 25}
                    if cursor
                    else {
                        **arguments,
                        "username": source["name"],
                        "include_shared_meetings": True,
                    }
                )
                result = unwrap(
                    await source["session"].call_tool("otter_search", query)
                )
                if not isinstance(result, dict) or not isinstance(
                    result.get("results"), list
                ):
                    raise TypeError("otter_search_shape_unsupported")
                ids.extend(meeting_id(x["id"]) for x in result["results"])
                cursor = result.get("next_cursor")
                reason = result.get("pagination_completion_reason")
                if not cursor:
                    complete = reason == "all_results_returned"
                    state["discovery_completion_reason"] = (
                        reason or "upstream_completion_unspecified"
                    )
                    break
                if cursor in seen:
                    state["discovery_completion_reason"] = "repeated_cursor"
                    break
                seen.add(cursor)
            else:
                state["discovery_completion_reason"] = "bounded_page_limit"
        except Exception as exc:  # noqa: BLE001 - preserve other accounts and partial discovery
            state["discovery_error_type"] = type(exc).__name__
        state["discovery_complete"] = complete
        receipt["discovery_complete"] &= complete
    return list(dict.fromkeys(ids))


async def fetch_from_sources(sources, cid):
    """A shared ID is one recording; another authorised account can supply it."""
    notice = None
    for source in sources:
        try:
            meeting = unwrap(
                await source["session"].call_tool("otter_fetch", {"id": cid})
            )
            if not isinstance(meeting, dict) or meeting.get("id") != cid:
                continue
            text = meeting.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if text.strip().casefold() == "no transcript available":
                notice = notice or (meeting, source["email"])
                continue
            return meeting, source["email"]
        except Exception:  # noqa: BLE001, S112 - try remaining authorised sources, then emit one typed failure
            continue
    if notice:
        return notice
    raise RuntimeError("otter_fetch_unavailable_from_connected_accounts")


@contextmanager
def state_db(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "state.sqlite3"
    db = sqlite3.connect(path, timeout=30)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, created TEXT, status TEXT,
          request TEXT, receipt TEXT, daily_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
    """)
    try:
        yield db
        db.commit()
    finally:
        db.close()


def meeting_id(value):
    if not isinstance(value, str):
        raise TypeError("meeting_id_must_be_string")
    if value.startswith("https://otter.ai/u/"):
        value = value.removeprefix("https://otter.ai/u/").split("?")[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid_otter_meeting_id")
    return value


def enqueue(
    resource_id,
    *,
    meeting_ids=None,
    created_after=None,
    created_before=None,
    participant_bindings=None,
    daily=False,
    spawn=True,
):
    _, root, _ = config(resource_id)
    ids = list(dict.fromkeys(meeting_id(x) for x in (meeting_ids or [])))
    if len(ids) > 25:
        raise ValueError("at_most_25_exact_meetings_per_request")
    for value in (created_after, created_before):
        if value:
            date.fromisoformat(value)
    if created_after and created_before and created_after > created_before:
        raise ValueError("invalid_date_range")
    bindings = participant_bindings or {}
    if not isinstance(bindings, dict) or len(bindings) > 80:
        raise ValueError("invalid_participant_bindings")
    # Bindings are case-sensitive meeting-local speaker labels -> reviewed person IDs.
    # A payload cannot select an account, executable, archive or acting user.
    for cid, labels in bindings.items():
        if cid not in ids or not isinstance(labels, dict):
            raise ValueError("bindings_require_an_exact_requested_meeting")
        for label, target in labels.items():
            if (
                not isinstance(label, str)
                or not label
                or len(label) > 200
                or not isinstance(target, str)
                or not target.startswith("#V#")
            ):
                raise ValueError("invalid_participant_binding")
    request = {
        "meeting_ids": ids,
        "created_after": created_after,
        "created_before": created_before,
        "participant_bindings": bindings,
    }
    run_id = uuid.uuid4().hex
    daily_key = datetime.now(UTC).date().isoformat() if daily else None
    with state_db(root) as db:
        db.executemany(
            "INSERT OR IGNORE INTO pending VALUES(?)", [(cid,) for cid in ids]
        )
        for cid, labels in bindings.items():
            db.execute(
                "INSERT OR REPLACE INTO state VALUES(?,?)",
                ("retry_bindings:" + cid, json.dumps(labels)),
            )
        prior = (
            db.execute(
                "SELECT id,status FROM jobs WHERE daily_key=?", (daily_key,)
            ).fetchone()
            if daily_key
            else None
        )
        if prior:
            run_id = prior[0]
        else:
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?)",
                (run_id, now(), "queued", json.dumps(request), "{}", daily_key),
            )
    if spawn:
        script = Path(__file__).resolve().parents[3] / "scripts" / "collect_otter.py"
        with (root / "worker.log").open("ab") as output:
            (root / "worker.log").chmod(0o600)
            subprocess.Popen(
                [sys.executable, str(script), "work", "--resource-id", resource_id],
                cwd=script.parent.parent,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
    return {
        "success": True,
        "run_id": run_id,
        "status": prior["status"] if prior else "queued",
        "collection_complete": bool(prior and prior["status"].startswith("completed")),
    }


def status(resource_id, run_id=None):
    _, root, settings = config(resource_id)
    if run_id and not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("invalid_run_id")
    with state_db(root) as db:
        rows = db.execute(
            "SELECT id,created,status,receipt FROM jobs WHERE (? IS NULL OR id=?) ORDER BY created DESC LIMIT 10",
            (run_id, run_id),
        ).fetchall()
        pending = db.execute("SELECT count(*) FROM pending").fetchone()[0]
    runs = []
    for row in rows:
        receipt = json.loads(row["receipt"])
        meetings = receipt.get("meetings", [])
        receipt["meetings"] = meetings[:20]
        receipt["omitted_meeting_receipts"] = max(0, len(meetings) - 20)
        runs.append({**dict(row), "receipt": receipt})
    return {
        "success": True,
        "runs": runs,
        "pending_meetings": pending,
        "schedule": settings.get("schedule", {}),
        "source_accounts": [
            x["email"] for x in source_accounts(config(resource_id)[0], settings)
        ],
        "coverage_note": "Date-window discovery; exact-ID refresh also supports older changed/shared meetings. Audio and screenshots are not collected.",
    }


def import_snapshot(archive, meeting):
    executable = (
        archive.project_dir
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    result = subprocess.run(
        [
            str(executable),
            "-m",
            "otter_archive_mcp.cli",
            "--database",
            str(archive.database),
            "import-live",
        ],
        input=json.dumps({"meeting": meeting, "retrieved_at": now()}),
        text=True,
        capture_output=True,
        env=archive.env,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("otter_archive_import_failed")
    receipt = json.loads(result.stdout)
    if (
        receipt.get("success") is not True
        or receipt.get("conversation_id") != meeting["id"]
    ):
        raise RuntimeError("otter_archive_import_unverified")
    return receipt


async def collect(resource_id, run_id, request):
    archive, root, settings = config(resource_id)
    receipt = {
        "started_at": now(),
        "counts": {"new": 0, "revised": 0, "unchanged": 0, "failed": 0},
        "meetings": [],
        "discovery_complete": True,
    }

    def checkpoint():
        with state_db(root) as db:
            db.execute(
                "UPDATE jobs SET receipt=? WHERE id=?", (json.dumps(receipt), run_id)
            )

    accounts = source_accounts(archive, settings)
    async with connected_sources(accounts, receipt) as sources:
        with state_db(root) as db:
            pending = [r[0] for r in db.execute("SELECT id FROM pending")]
            watermark = db.execute(
                "SELECT value FROM state WHERE key='last_complete_date'"
            ).fetchone()
        ids = list(dict.fromkeys(request["meeting_ids"] + pending))
        if not request["meeting_ids"]:
            end = (
                date.fromisoformat(request["created_before"])
                if request.get("created_before")
                else datetime.now(UTC).date()
            )
            start = (
                date.fromisoformat(request["created_after"])
                if request.get("created_after")
                else min(
                    end - timedelta(days=7),
                    date.fromisoformat(watermark[0]) if watermark else end,
                )
            )
            receipt["date_window"] = {"from": start.isoformat(), "to": end.isoformat()}
            ids.extend(
                await discover_sources(
                    sources,
                    {
                        "created_after": start.strftime("%Y/%m/%d"),
                        "created_before": end.strftime("%Y/%m/%d"),
                        "page_size": 25,
                    },
                    receipt,
                )
            )
        ids = list(dict.fromkeys(ids))
        # Persist all discovered IDs before processing any item. Failures survive windows.
        with state_db(root) as db:
            db.executemany(
                "INSERT OR IGNORE INTO pending VALUES(?)", [(cid,) for cid in ids]
            )
        checkpoint()

        async def collect_item(cid):
            try:
                current_archive, _, current_settings = config(resource_id)
                if (
                    current_archive.owner_user_concept_id
                    != archive.owner_user_concept_id
                    or source_accounts(current_archive, current_settings) != accounts
                ):
                    raise PermissionError("otter_collection_binding_changed")
                meeting, source_email = await fetch_from_sources(sources, cid)
                archived = await asyncio.to_thread(import_snapshot, archive, meeting)
                archived["collected_via_account"] = source_email
                from .otter_representation_service import represent_meeting

                with state_db(root) as db:
                    retry_bindings = db.execute(
                        "SELECT value FROM state WHERE key=?",
                        ("retry_bindings:" + cid,),
                    ).fetchone()
                bindings = json.loads(retry_bindings[0]) if retry_bindings else {}
                represented = await asyncio.to_thread(
                    represent_meeting,
                    archive.owner_user_concept_id,
                    meeting,
                    archived,
                    bindings,
                )
                available = archived.get("transcript_available", True)
                receipt["counts"][archived["status"] if available else "failed"] += 1
                receipt["meetings"].append({**archived, **represented})
                if not available:
                    receipt["meetings"][-1]["error_code"] = (
                        "otter_transcript_unavailable"
                    )
                    checkpoint()
                    return
                with state_db(root) as db:
                    db.execute("DELETE FROM pending WHERE id=?", (cid,))
                    db.execute(
                        "DELETE FROM state WHERE key=?", ("retry_bindings:" + cid,)
                    )
            except Exception as exc:  # noqa: BLE001 - durable per-item recovery and redacted receipts
                receipt["counts"]["failed"] += 1
                # Exception messages may contain upstream transcript/credentials.
                receipt["meetings"].append(
                    {
                        "conversation_id": cid,
                        "error_type": type(exc).__name__,
                        "error_code": str(exc)
                        if re.fullmatch(r"[a-z_]{1,100}", str(exc))
                        else "collection_item_failed",
                    }
                )
            checkpoint()

        # Independent recordings use disjoint identities; bound upstream/Atlas load.
        slots = asyncio.Semaphore(3)

        async def bounded_item(cid):
            async with slots:
                await collect_item(cid)

        await asyncio.gather(*(bounded_item(cid) for cid in ids))
        if (
            receipt["discovery_complete"]
            and not receipt["counts"]["failed"]
            and not request["meeting_ids"]
        ):
            with state_db(root) as db:
                db.execute(
                    "INSERT OR REPLACE INTO state VALUES('last_complete_date',?)",
                    (end.isoformat(),),
                )
    receipt["finished_at"] = now()
    receipt["collection_complete"] = not receipt["counts"]["failed"]
    receipt["collection_scope"] = (
        "returned_meetings"
        if not receipt["discovery_complete"]
        else "requested_meetings"
    )
    return receipt


def work(resource_id):
    import fcntl  # Host worker supports macOS/Linux; no network lock service is needed.

    _, root, _ = config(resource_id)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "worker.lock").open("a") as lock:
        # Wait for the previous worker, then drain requests that arrived during it.
        fcntl.flock(lock, fcntl.LOCK_EX)
        with state_db(root) as db:
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        while True:
            with state_db(root) as db:
                job = db.execute(
                    "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
                ).fetchone()
                if not job:
                    break
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
            try:
                receipt = asyncio.run(
                    collect(resource_id, job["id"], json.loads(job["request"]))
                )
                terminal = (
                    (
                        "completed"
                        if receipt["discovery_complete"]
                        else "completed_with_coverage_limit"
                    )
                    if receipt["collection_complete"]
                    else "partial"
                )
            except Exception as exc:  # noqa: BLE001 - durable per-item recovery and redacted receipts
                with state_db(root) as db:
                    receipt = json.loads(
                        db.execute(
                            "SELECT receipt FROM jobs WHERE id=?", (job["id"],)
                        ).fetchone()[0]
                    )
                receipt.update(
                    {
                        "collection_complete": False,
                        "finished_at": now(),
                        "error_type": type(exc).__name__,
                        "error_code": "otter_connection_or_collection_failed",
                    }
                )
                terminal = "failed"
            with state_db(root) as db:
                db.execute(
                    "UPDATE jobs SET status=?,receipt=? WHERE id=?",
                    (terminal, json.dumps(receipt), job["id"]),
                )
    return status(resource_id)
