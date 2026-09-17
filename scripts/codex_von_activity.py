"""Bounded, resumable capture for the existing DGX controller.

Never reads authentication files or discovers sessions. Only named companion
files in one controller-owned run directory are eligible. Local originals stay
untouched. Opaque reasoning is removed, never interpreted or decrypted.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from collections import Counter
from pathlib import Path

FILES = (
    "context.json",
    "schema.json",
    "result.json",
    "exit.json",
    "stderr.log",
    "launch-error.json",
    "deployment.json",
    "deployment.log",
)
REQUIRED_FILES = {"context.json", "events.jsonl", "exit.json", "result.json"}
BATCH_TARGET = 512 * 1024
MAX_SOURCE_FILE = 128 * 1024 * 1024


def service():
    from src.backend.services import task_run_archive_service

    return task_run_archive_service


def redact(value, counts):
    from src.backend.services.turn_failure_capsule_service import _SECRET_PATTERNS

    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"encrypted_content", "redacted_thinking"}:
                counts["opaque_reasoning_omitted"] += 1
                result[key] = "[opaque provider state omitted]"
            elif re.fullmatch(
                r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|password|passwd|secret|client[_-]?secret|cookie|authorization)",
                key,
            ):
                counts["credential_field"] += 1
                result[key] = "[redacted]"
            else:
                result[key] = redact(item, counts)
        return result
    if isinstance(value, list):
        return [redact(item, counts) for item in value]
    if isinstance(value, str):
        # Exact controller-provided credentials, without reading credential
        # files or copying their values into checkpoints or manifests.
        for name, secret in os.environ.items():
            if (
                re.search(r"(?i)(secret|token|password|api_key|mongo_uri)", name)
                and len(secret) >= 8
            ):
                count = value.count(secret)
                if count:
                    value = value.replace(secret, "[redacted]")
                    counts["known_credential"] += count
        # Existing credential patterns only; preserve source paths/trace frames.
        for pattern, replacement in _SECRET_PATTERNS[:7]:
            value, count = pattern.subn(replacement, value)
            counts["credential_pattern"] += count
        return value
    return value


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def sanitise_lines(raw):
    counts = Counter()
    output = []
    thread_id = None
    summaries = 0
    invalid = 0
    # split on LF only; other Unicode separators belong to the event content.
    lines = raw.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    for line in lines:
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            invalid += 1
            value = {
                "type": "capture.invalid_record",
                "source_text": line.decode("utf-8", errors="replace"),
            }
        if isinstance(value, dict):
            if value.get("type") == "thread.started":
                thread_id = value.get("thread_id")
            item = value.get("item", {})
            if (
                isinstance(item, dict)
                and item.get("type") == "reasoning"
                and (item.get("text") or item.get("summary"))
            ):
                summaries += 1
        output.append(encode(redact(value, counts)))
    return b"".join(output), {
        "redactions": dict(counts),
        "event_count": len(lines),
        "invalid_records": invalid,
        "exposed_summary_records": summaries,
        "thread_id": thread_id,
    }


def read_batch(path, start, *, terminal=False):
    if not path.exists():
        return b""
    if path.is_symlink():
        raise ValueError("Capture source must not be a symlink")
    with path.open("rb") as stream:
        stream.seek(start)
        raw = stream.read(BATCH_TARGET)
        if not raw:
            return b""
        if not raw.endswith(b"\n"):
            rest = stream.readline(service().MAX_BATCH_BYTES - len(raw) + 1)
            raw += rest
        if len(raw) > service().MAX_BATCH_BYTES:
            raise ValueError(
                "Single activity record exceeds capture bound; source retained locally"
            )
        if not terminal and not raw.endswith(b"\n"):
            end = raw.rfind(b"\n")
            return raw[: end + 1] if end >= 0 else b""
        return raw


def _context(state):
    path = Path(state["run_dir"]) / "context.json"
    if path.is_symlink() or path.stat().st_size > MAX_SOURCE_FILE:
        raise ValueError("Invalid context capture source")
    context = json.loads(path.read_text())
    if context.get("task_id") != state["task_id"]:
        raise ValueError("Run source does not match selected task")
    return context


def checkpoint(config, state, *, terminal=False):
    api = service()
    run_dir = Path(state["run_dir"])
    if run_dir.is_symlink() or run_dir.name != state["attempt"]:
        raise ValueError("Run directory does not match attempt identity")
    context = _context(state)
    conversation = context.get("conversation", {})
    if conversation.get("available") and not conversation.get("session_id"):
        # Legacy transcript pages have no stable common locator. Do not guess
        # or broaden their audience in historical backfill.
        raise ValueError(
            "Available source conversation needs its exact session identity"
        )
    provenance = state.get("capture_provenance")
    if not provenance:
        provenance = {
            "started_at": state.get("started_at"),
            "execution_settings": context.get("execution_settings", {}),
            "worker_id": context.get("worker_identity"),
            "capture_worker_script_sha256": api.digest(
                Path(__file__).with_name("codex_von_worker.py").read_bytes()
            ),
            "capture_script_sha256": api.digest(Path(__file__).read_bytes()),
            "source": "codex exec --json and explicitly named run companions",
            "original_worker_revision": state.get(
                "worker_revision", "not recorded by original launcher"
            ),
            "backfill": bool(state.get("backfill")),
        }
        state["capture_provenance"] = provenance
    row = api.register_run(
        task_id=state["task_id"],
        run_id=state["attempt"],
        provenance=provenance,
        source_session_id=(
            conversation.get("session_id") if conversation.get("available") else None
        ),
    )
    source = run_dir / "events.jsonl"
    if source.exists() and source.stat().st_size < row["cursor"]:
        raise ValueError("Local activity source shrank below published cursor")
    # At most four batches per live checkpoint; terminal catches up completely.
    batches = 0
    while terminal or batches < 4:
        raw = read_batch(source, row["cursor"], terminal=terminal)
        if not raw:
            break
        data, info = sanitise_lines(raw)
        row = api.append_batch(
            state["attempt"],
            start=row["cursor"],
            end=row["cursor"] + len(raw),
            data=data,
            source_sha256=api.digest(raw),
            event_count=info["event_count"],
            thread_id=info["thread_id"],
        )
        batches += 1
    state["capture"] = {
        "status": row["capture_status"],
        "cursor": row["cursor"],
        "activity_url": row["activity_url"],
        "updated_at": row["updated_at"],
    }
    if not terminal:
        return row
    if row["capture_status"] == "complete":
        # Verify the durable bytes on recovery, not just a persisted status.
        api.download_archive(state["attempt"], task_id=state["task_id"])
        state["capture"]["archive_url"] = row["archive_url"]
        state["capture"]["source_complete"] = row["manifest"]["source_complete"]
        return row
    return finalise(state, row)


def finalise(state, row):
    api = service()
    run_dir = Path(state["run_dir"])
    entries = []
    payloads = {}
    redactions = Counter()
    event_info = Counter()
    source_total = 0
    for name in ("events.jsonl", *FILES):
        path = run_dir / name
        if not path.exists():
            continue
        if path.is_symlink() or path.stat().st_size > MAX_SOURCE_FILE:
            raise ValueError(
                "Source file exceeds bounded archive route or is a symlink"
            )
        raw = path.read_bytes()
        source_total += len(raw)
        if source_total > api.MAX_ARCHIVE_BYTES:
            raise ValueError("Run exceeds bounded archive route; originals retained")
        if name == "events.jsonl":
            if len(raw) != row["cursor"]:
                raise ValueError("Source changed during finalisation")
            for segment in row["capture_segments"]:
                if (
                    api.digest(raw[segment["start"] : segment["end"]])
                    != segment["source_sha256"]
                ):
                    raise ValueError(
                        "Published source segment changed before finalisation"
                    )
            data, info = sanitise_lines(raw)
            redactions.update(info["redactions"])
            event_info.update(
                {
                    key: info[key]
                    for key in (
                        "event_count",
                        "invalid_records",
                        "exposed_summary_records",
                    )
                }
            )
        else:
            counts = Counter()
            try:
                value = (
                    json.loads(raw) if name.endswith(".json") else raw.decode("utf-8")
                )
            except (ValueError, UnicodeDecodeError):
                value = raw.decode("utf-8", errors="replace")
                counts["invalid_source_encoding_or_json"] += 1
            cleaned = redact(value, counts)
            data = encode(cleaned) if name.endswith(".json") else cleaned.encode()
            redactions.update(counts)
        payloads[name] = data
        entries.append(
            {
                "name": name,
                "source_bytes": len(raw),
                "source_sha256": api.digest(raw),
                "bytes": len(data),
                "sha256": api.digest(data),
            }
        )
    # An absent event file is an explicit incomplete source, not an omitted ZIP member.
    if "events.jsonl" not in payloads:
        payloads["events.jsonl"] = b""
        entries.append(
            {
                "name": "events.jsonl",
                "source_bytes": 0,
                "source_sha256": api.digest(b""),
                "bytes": 0,
                "sha256": api.digest(b""),
            }
        )
    missing = sorted(
        REQUIRED_FILES - {p.name for p in run_dir.iterdir() if p.is_file()}
    )
    manifest = {
        "schema_version": "von_task_run_archive.v1",
        "run_id": state["attempt"],
        "task_id": state["task_id"],
        "thread_id": row["thread_id"],
        "provenance": row["provenance"],
        "finished_at": state.get("finished_at") or api.now(),
        "outcome": state.get("result", {}).get("status", "interrupted"),
        "source_segments": row["capture_segments"],
        "final_cursor": row["cursor"],
        "event_count": event_info["event_count"],
        "files": entries,
        "missing_files": missing,
        "source_complete": not missing
        and not event_info["invalid_records"]
        and not redactions["invalid_source_encoding_or_json"],
        "invalid_records": event_info["invalid_records"],
        "redactions": dict(redactions),
        "reasoning_summaries": {
            "exposed_records": event_info["exposed_summary_records"],
            "availability": (
                "exposed"
                if event_info["exposed_summary_records"]
                else "not exposed in captured exec stream"
            ),
        },
        "limitations": [
            "Only provider-exposed exec activity and listed companion files are captured.",
            "No hidden chain of thought is extracted, decrypted or claimed.",
            "Source rollout not collected: no supported bounded export route is configured; no session discovery is performed.",
            "Credential patterns and known controller credentials are redacted; opaque provider state is omitted.",
            "The readable view is limited; archived redacted records retain available command outputs without display truncation.",
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, data in payloads.items():
            package.writestr(name, data)
        package.writestr("manifest.json", encode(manifest))
    verified = api.finalise_run(
        state["attempt"], archive=buffer.getvalue(), manifest=manifest
    )
    state["capture"] = {
        "status": "complete",
        "cursor": verified["cursor"],
        "archive_url": verified["archive_url"],
        "activity_url": verified["activity_url"],
        "source_complete": manifest["source_complete"],
        "updated_at": verified["updated_at"],
    }
    return verified


def try_checkpoint(config, state, *, terminal=False):
    try:
        checkpoint(config, state, terminal=terminal)
        return True
    except Exception as exc:  # noqa: BLE001
        # Archive failures must preserve coding progress.
        # Retain a safe error class, never a storage URL/credential-bearing exception.
        state["capture"] = state.get("capture", {}) | {
            "status": "pending",
            "error_type": type(exc).__name__,
        }
        try:
            service().capture_failure(state["attempt"], type(exc).__name__)
        except Exception as report_exc:  # noqa: BLE001 - retain safe diagnostic only
            state["capture"]["failure_report_error_type"] = type(report_exc).__name__
        return False
