"""Durable, actor-scoped import of allowlisted local agent conversations."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from pymongo import ASCENDING, DESCENDING, ReturnDocument

from ..db.mongo_client import get_db
from ..db.transient_errors import is_transient_mongo_error
from .external_conversation_import_service import (
    ExternalConversationImportError,
    import_external_conversation_file,
)

logger = logging.getLogger(__name__)

BATCH_COLLECTION = "external_conversation_import_batches"
ITEM_COLLECTION = "external_conversation_import_items"
SUPPORTED_PROVIDERS = ("codex", "claude_code", "copilot", "gemini")
TERMINAL_ITEM_STATES = ("completed", "failed", "unsupported")
TERMINAL_BATCH_STATES = ("completed", "partial", "failed", "cancelled")
LEASE_SECONDS = 90
MAX_ATTEMPTS = 5

_INDEX_LOCK = threading.Lock()
_INDEXED_DATABASES: set[str] = set()
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD: threading.Thread | None = None
_WORKER_STOP = threading.Event()
_WORKER_WAKE = threading.Event()


class ExternalConversationBulkImportError(ValueError):
    def __init__(self, message: str, *, error_code: str, status_code: int = 400):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class _RetryableItemError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalConversationSource:
    provider: str
    source_key: str
    path: Path
    root_id: str
    size_bytes: int
    modified_at_utc: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _collections():
    db = get_db()
    if db is None:
        raise RuntimeError("MongoDB is unavailable for durable conversation import.")
    database_key = str(getattr(db, "name", "default"))
    if database_key not in _INDEXED_DATABASES:
        with _INDEX_LOCK:
            if database_key not in _INDEXED_DATABASES:
                batches = db[BATCH_COLLECTION]
                items = db[ITEM_COLLECTION]
                batches.create_index([("batch_id", ASCENDING)], unique=True)
                batches.create_index(
                    [("custodian_user_id", ASCENDING), ("created_at", DESCENDING)]
                )
                batches.create_index([("status", ASCENDING), ("updated_at", ASCENDING)])
                items.create_index(
                    [("batch_id", ASCENDING), ("source_key", ASCENDING)], unique=True
                )
                items.create_index(
                    [
                        ("batch_id", ASCENDING),
                        ("status", ASCENDING),
                        ("next_retry_at", ASCENDING),
                        ("lease_expires_at", ASCENDING),
                    ]
                )
                _INDEXED_DATABASES.add(database_key)
    return db[BATCH_COLLECTION], db[ITEM_COLLECTION]


def _normalise_providers(values: Sequence[Any] | None) -> tuple[str, ...]:
    if not values:
        return SUPPORTED_PROVIDERS
    providers: list[str] = []
    aliases = {"claude": "claude_code", "vscode": "copilot"}
    for value in values:
        provider = aliases.get(str(value).strip().lower(), str(value).strip().lower())
        if provider not in SUPPORTED_PROVIDERS:
            raise ExternalConversationBulkImportError(
                f"Unsupported local conversation provider: {value}",
                error_code="unsupported_external_conversation_provider",
            )
        if provider not in providers:
            providers.append(provider)
    return tuple(providers)


def _provider_roots(provider: str) -> tuple[tuple[str, Path, str], ...]:
    home = Path.home()
    overrides = {
        "codex": os.getenv("VON_CODEX_CONVERSATION_ROOT"),
        "claude_code": os.getenv("VON_CLAUDE_CONVERSATION_ROOT"),
        "copilot": os.getenv("VON_COPILOT_CONVERSATION_ROOT"),
        "gemini": os.getenv("VON_GEMINI_CONVERSATION_ROOT"),
    }
    override = overrides.get(provider)
    if override:
        pattern = "**/*.json*" if provider == "copilot" else "**/*.jsonl"
        return ((f"{provider}:configured", Path(override).expanduser(), pattern),)
    if provider == "codex":
        return (("codex:sessions", home / ".codex" / "sessions", "**/*.jsonl"),)
    if provider == "claude_code":
        return (("claude:projects", home / ".claude" / "projects", "**/*.jsonl"),)
    if provider == "gemini":
        return (("gemini:chats", home / ".gemini" / "tmp", "*/chats/*.jsonl"),)
    application_support = home / "Library" / "Application Support"
    return tuple(
        (
            f"copilot:{name}",
            application_support / name / "User" / "workspaceStorage",
            "**/chatSessions/*.json*",
        )
        for name in ("Code", "Code - Insiders")
    ) + tuple(
        (
            f"copilot:{name}:empty",
            application_support / name / "User" / "globalStorage",
            "**/emptyWindowChatSessions/*.json*",
        )
        for name in ("Code", "Code - Insiders")
    )


def discover_local_conversations(
    providers: Sequence[Any] | None = None,
) -> tuple[list[LocalConversationSource], list[dict[str, str]]]:
    """Discover only server-configured roots; callers never supply paths."""

    sources: list[LocalConversationSource] = []
    warnings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for provider in _normalise_providers(providers):
        for root_id, configured_root, pattern in _provider_roots(provider):
            try:
                root = configured_root.resolve(strict=True)
            except OSError:
                warnings.append({"provider": provider, "code": "root_unavailable"})
                continue
            if not root.is_dir():
                warnings.append({"provider": provider, "code": "root_not_directory"})
                continue
            for candidate in root.glob(pattern):
                try:
                    resolved = candidate.resolve(strict=True)
                    if root not in resolved.parents or not resolved.is_file():
                        warnings.append(
                            {
                                "provider": provider,
                                "code": "path_outside_allowlisted_root",
                            }
                        )
                        continue
                    stat = resolved.stat()
                except OSError:
                    warnings.append({"provider": provider, "code": "source_unreadable"})
                    continue
                identity = (provider, str(resolved))
                if identity in seen or stat.st_size <= 0:
                    continue
                seen.add(identity)
                source_key = hashlib.sha256(
                    f"{provider}|{resolved}".encode("utf-8")
                ).hexdigest()
                sources.append(
                    LocalConversationSource(
                        provider=provider,
                        source_key=source_key,
                        path=resolved,
                        root_id=root_id,
                        size_bytes=int(stat.st_size),
                        modified_at_utc=datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc
                        ).isoformat(),
                    )
                )
    sources.sort(
        key=lambda item: (item.provider, item.modified_at_utc, item.source_key)
    )
    return sources, warnings


def preview_local_conversation_import(
    *, providers: Sequence[Any] | None = None
) -> dict[str, Any]:
    sources, warnings = discover_local_conversations(providers)
    counts = {provider: 0 for provider in SUPPORTED_PROVIDERS}
    bytes_by_provider = {provider: 0 for provider in SUPPORTED_PROVIDERS}
    for source in sources:
        counts[source.provider] += 1
        bytes_by_provider[source.provider] += source.size_bytes
    return {
        "schema_version": "external_conversation_bulk_import_preview.v1",
        "read_only": True,
        "source_count": len(sources),
        "source_bytes": sum(source.size_bytes for source in sources),
        "providers": {
            provider: {
                "source_count": counts[provider],
                "source_bytes": bytes_by_provider[provider],
            }
            for provider in _normalise_providers(providers)
        },
        "warnings": warnings[:100],
        "warning_count": len(warnings),
    }


def _populate_batch(batch: Mapping[str, Any]) -> None:
    batches, items = _collections()
    batch_id = str(batch["batch_id"])
    sources, warnings = discover_local_conversations(batch.get("providers"))
    now = _now()
    for offset in range(0, len(sources), 500):
        documents = []
        for source in sources[offset : offset + 500]:
            documents.append(
                {
                    "item_id": str(uuid.uuid4()),
                    "batch_id": batch_id,
                    "custodian_user_id": batch["custodian_user_id"],
                    "provider": source.provider,
                    "source_key": source.source_key,
                    "root_id": source.root_id,
                    "local_path": str(source.path),
                    "source_size_bytes": source.size_bytes,
                    "source_modified_at_utc": source.modified_at_utc,
                    "status": "pending",
                    "attempt_count": 0,
                    "raw_checkpoint": None,
                    "projection_checkpoint": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        if documents:
            try:
                items.insert_many(documents, ordered=False)
            except Exception as exc:
                if "duplicate key" not in str(exc).lower():
                    raise
    total = items.count_documents({"batch_id": batch_id})
    batches.update_one(
        {"batch_id": batch_id, "status": "planning"},
        {
            "$set": {
                "status": "ready" if total else "completed",
                "source_count": total,
                "source_bytes": sum(source.size_bytes for source in sources),
                "discovery_warnings": warnings[:100],
                "discovery_warning_count": len(warnings),
                "planning_completed_at": now,
                "updated_at": now,
            }
        },
    )
    batches.update_one(
        {"batch_id": batch_id, "status": "pause_requested"},
        {"$set": {"status": "paused", "updated_at": now}},
    )


def start_local_conversation_import(
    *,
    custodian_user_id: str,
    namespace: str | None,
    organisation_concept_id: str | None,
    role_in_org: str | None,
    providers: Sequence[Any] | None = None,
) -> dict[str, Any]:
    if not custodian_user_id:
        raise ExternalConversationBulkImportError(
            "An authenticated actor is required.",
            error_code="not_authenticated",
            status_code=401,
        )
    batches, _items = _collections()
    batch_id = str(uuid.uuid4())
    now = _now()
    batch = {
        "batch_id": batch_id,
        "schema_version": "external_conversation_bulk_import.v1",
        "custodian_user_id": custodian_user_id,
        "namespace": namespace,
        "organisation_concept_id": organisation_concept_id,
        "role_in_org": role_in_org,
        "providers": list(_normalise_providers(providers)),
        "status": "planning",
        "source_count": 0,
        "source_bytes": 0,
        "created_at": now,
        "updated_at": now,
    }
    batches.insert_one(batch)
    try:
        _populate_batch(batch)
    except Exception as exc:
        batches.update_one(
            {"batch_id": batch_id},
            {
                "$set": {
                    "status": "failed",
                    "error_code": "discovery_failed",
                    "updated_at": _now(),
                }
            },
        )
        raise ExternalConversationBulkImportError(
            f"Local conversation discovery failed: {exc}",
            error_code="external_conversation_discovery_failed",
            status_code=500,
        ) from exc
    start_external_conversation_bulk_import_worker()
    _WORKER_WAKE.set()
    return get_local_conversation_import_batch(
        custodian_user_id=custodian_user_id, batch_id=batch_id
    )


def _counts_for_batch(batch_id: str) -> dict[str, int]:
    _batches, items = _collections()
    counts = {
        state: 0
        for state in (*TERMINAL_ITEM_STATES, "pending", "running", "retry_wait")
    }
    for row in items.aggregate(
        [
            {"$match": {"batch_id": batch_id}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]
    ):
        if isinstance(row.get("_id"), str):
            counts[row["_id"]] = int(row.get("count", 0))
    counts["diverged"] = items.count_documents(
        {
            "batch_id": batch_id,
            "result.divergence": {"$exists": True, "$ne": None},
        }
    )
    return counts


def _safe_batch(batch: Mapping[str, Any], counts: Mapping[str, int]) -> dict[str, Any]:
    return {
        key: batch.get(key)
        for key in (
            "schema_version",
            "batch_id",
            "status",
            "providers",
            "source_count",
            "source_bytes",
            "discovery_warning_count",
            "created_at",
            "updated_at",
            "planning_completed_at",
            "completed_at",
            "error_code",
        )
    } | {"counts": dict(counts), "restartable": True, "paths_exposed": False}


def get_local_conversation_import_batch(
    *, custodian_user_id: str, batch_id: str
) -> dict[str, Any]:
    batches, _items = _collections()
    batch = batches.find_one(
        {"batch_id": batch_id, "custodian_user_id": custodian_user_id}
    )
    if not isinstance(batch, Mapping):
        raise ExternalConversationBulkImportError(
            "Import batch not found.",
            error_code="external_conversation_batch_not_found",
            status_code=404,
        )
    return _safe_batch(batch, _counts_for_batch(batch_id))


def list_local_conversation_import_batches(
    *, custodian_user_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    batches, _items = _collections()
    return [
        _safe_batch(batch, _counts_for_batch(str(batch["batch_id"])))
        for batch in batches.find({"custodian_user_id": custodian_user_id})
        .sort("created_at", DESCENDING)
        .limit(max(1, min(limit, 100)))
    ]


def list_local_conversation_import_items(
    *, custodian_user_id: str, batch_id: str, offset: int = 0, limit: int = 100
) -> dict[str, Any]:
    get_local_conversation_import_batch(
        custodian_user_id=custodian_user_id, batch_id=batch_id
    )
    _batches, items = _collections()
    bounded_limit = max(1, min(limit, 200))
    rows = (
        items.find(
            {"batch_id": batch_id, "custodian_user_id": custodian_user_id},
            {
                "_id": 0,
                "item_id": 1,
                "batch_id": 1,
                "provider": 1,
                "source_key": 1,
                "root_id": 1,
                "source_size_bytes": 1,
                "source_modified_at_utc": 1,
                "status": 1,
                "attempt_count": 1,
                "raw_checkpoint": 1,
                "projection_checkpoint": 1,
                "result": 1,
                "error_code": 1,
                "created_at": 1,
                "updated_at": 1,
                "completed_at": 1,
            },
        )
        .sort([("provider", ASCENDING), ("source_modified_at_utc", ASCENDING)])
        .skip(max(0, offset))
        .limit(bounded_limit)
    )
    return {
        "batch_id": batch_id,
        "offset": max(0, offset),
        "limit": bounded_limit,
        "items": list(rows),
    }


def control_local_conversation_import(
    *, custodian_user_id: str, batch_id: str, action: str
) -> dict[str, Any]:
    batches, _items = _collections()
    now = _now()
    if action == "pause":
        query = {"status": {"$in": ["planning", "ready", "running"]}}
        status = "pause_requested"
    elif action == "resume":
        query = {"status": {"$in": ["paused", "pause_requested"]}}
        status = "ready"
    elif action == "cancel":
        query = {"status": {"$nin": list(TERMINAL_BATCH_STATES)}}
        status = "cancelled"
    else:
        raise ExternalConversationBulkImportError(
            "Action must be pause, resume, or cancel.",
            error_code="external_conversation_batch_action_invalid",
        )
    result = batches.update_one(
        {"batch_id": batch_id, "custodian_user_id": custodian_user_id, **query},
        {"$set": {"status": status, "updated_at": now}},
    )
    if not result.matched_count:
        get_local_conversation_import_batch(
            custodian_user_id=custodian_user_id, batch_id=batch_id
        )
    if action == "resume":
        start_external_conversation_bulk_import_worker()
    _WORKER_WAKE.set()
    return get_local_conversation_import_batch(
        custodian_user_id=custodian_user_id, batch_id=batch_id
    )


def _claim_item() -> Mapping[str, Any] | None:
    batches, items = _collections()
    now = _now()
    planning_batch = batches.find_one(
        {"status": "planning"}, sort=[("created_at", ASCENDING)]
    )
    if isinstance(planning_batch, Mapping):
        _populate_batch(planning_batch)
    for batch in (
        batches.find({"status": {"$in": ["ready", "running"]}})
        .sort("created_at", ASCENDING)
        .limit(20)
    ):
        batch_id = str(batch["batch_id"])
        token = str(uuid.uuid4())
        item = items.find_one_and_update(
            {
                "batch_id": batch_id,
                "$or": [
                    {"status": "pending"},
                    {"status": "retry_wait", "next_retry_at": {"$lte": now}},
                    {"status": "running", "lease_expires_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "status": "running",
                    "lease_token": token,
                    "lease_expires_at": now + timedelta(seconds=LEASE_SECONDS),
                    "heartbeat_at": now,
                    "updated_at": now,
                },
                "$inc": {"attempt_count": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if item is not None:
            batches.update_one(
                {"batch_id": batch_id, "status": {"$in": ["ready", "running"]}},
                {"$set": {"status": "running", "updated_at": now}},
            )
            return item
        _settle_batch(batch_id)
    return None


def _settle_batch(batch_id: str) -> None:
    batches, items = _collections()
    batch = batches.find_one({"batch_id": batch_id})
    if not isinstance(batch, Mapping):
        return
    running = items.count_documents({"batch_id": batch_id, "status": "running"})
    if batch.get("status") == "pause_requested" and running == 0:
        batches.update_one(
            {"batch_id": batch_id, "status": "pause_requested"},
            {"$set": {"status": "paused", "updated_at": _now()}},
        )
        return
    unfinished = items.count_documents(
        {"batch_id": batch_id, "status": {"$in": ["pending", "running", "retry_wait"]}}
    )
    if unfinished or batch.get("status") in {
        "paused",
        "pause_requested",
        "cancelled",
        "planning",
    }:
        return
    failed = items.count_documents(
        {"batch_id": batch_id, "status": {"$in": ["failed", "unsupported"]}}
    )
    completed = items.count_documents({"batch_id": batch_id, "status": "completed"})
    diverged = items.count_documents(
        {
            "batch_id": batch_id,
            "result.divergence": {"$exists": True, "$ne": None},
        }
    )
    status = (
        "completed"
        if failed == 0 and diverged == 0
        else ("partial" if completed else "failed")
    )
    batches.update_one(
        {"batch_id": batch_id, "status": {"$in": ["ready", "running"]}},
        {"$set": {"status": status, "completed_at": _now(), "updated_at": _now()}},
    )


def _heartbeat(item: Mapping[str, Any], stop: threading.Event) -> None:
    _batches, items = _collections()
    while not stop.wait(LEASE_SECONDS / 3):
        now = _now()
        result = items.update_one(
            {
                "item_id": item["item_id"],
                "lease_token": item["lease_token"],
                "status": "running",
            },
            {
                "$set": {
                    "heartbeat_at": now,
                    "lease_expires_at": now + timedelta(seconds=LEASE_SECONDS),
                    "updated_at": now,
                }
            },
        )
        if not result.matched_count:
            return


def _safe_item_result(result: Mapping[str, Any]) -> dict[str, Any]:
    projection_value = result.get("projection")
    projection: Mapping[str, Any] = (
        projection_value if isinstance(projection_value, Mapping) else {}
    )
    raw_value = result.get("raw_ingestion")
    raw: Mapping[str, Any] = raw_value if isinstance(raw_value, Mapping) else {}
    return {
        "status": result.get("status"),
        "session_id": projection.get("session_id"),
        "projection_action": projection.get("action"),
        "appended_message_count": projection.get("appended_message_count"),
        "divergence": projection.get("divergence"),
        "raw_action": raw.get("action"),
        "raw_document_concept_id": raw.get("document_concept_id"),
    }


def _process_item(item: Mapping[str, Any]) -> None:
    batches, items = _collections()
    batch = batches.find_one({"batch_id": item["batch_id"]})
    if not isinstance(batch, Mapping):
        return
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat, args=(item, heartbeat_stop), daemon=True
    )
    heartbeat.start()

    def _checkpoint(phase: str, payload: Mapping[str, Any]) -> None:
        field = {
            "raw_ingested": "raw_checkpoint",
            "projection_reconciled": "projection_checkpoint",
        }.get(phase)
        if field is None:
            raise RuntimeError("unknown_external_conversation_import_checkpoint")
        checkpoint_result = items.update_one(
            {
                "item_id": item["item_id"],
                "lease_token": item["lease_token"],
                "status": "running",
            },
            {"$set": {field: dict(payload), "updated_at": _now()}},
        )
        if not checkpoint_result.matched_count:
            raise RuntimeError("external_conversation_import_lease_lost")

    try:
        result = import_external_conversation_file(
            path=Path(str(item["local_path"])),
            custodian_user_id=str(batch["custodian_user_id"]),
            namespace=batch.get("namespace"),
            organisation_concept_id=batch.get("organisation_concept_id"),
            role_in_org=batch.get("role_in_org"),
            provider_hint=str(item["provider"]),
            dry_run=False,
            checkpoint_callback=_checkpoint,
        )
        if not result.get("success"):
            error_message = str(
                result.get("error_code") or result.get("status") or "import_failed"
            )
            if result.get("status") in {
                "raw_source_ingestion_failed",
                "conversation_projection_failed",
            }:
                raise _RetryableItemError(error_message)
            raise RuntimeError(error_message)
        raw_value = result.get("raw_ingestion")
        raw: Mapping[str, Any] = raw_value if isinstance(raw_value, Mapping) else {}
        projection_value = result.get("projection")
        projection: Mapping[str, Any] = (
            projection_value if isinstance(projection_value, Mapping) else {}
        )
        preview_value = result.get("preview")
        preview: Mapping[str, Any] = (
            preview_value if isinstance(preview_value, Mapping) else {}
        )
        items.update_one(
            {
                "item_id": item["item_id"],
                "lease_token": item["lease_token"],
                "status": "running",
            },
            {
                "$set": {
                    "status": "completed",
                    "raw_checkpoint": {
                        "completed": True,
                        "source_sha256": preview.get("source_sha256"),
                        "action": raw.get("action"),
                        "document_concept_id": raw.get("document_concept_id"),
                        "file_copy_concept_id": raw.get("file_copy_concept_id"),
                    },
                    "projection_checkpoint": {
                        "completed": True,
                        "source_sha256": preview.get("source_sha256"),
                        "package_sha256": preview.get("package_sha256"),
                        "action": projection.get("action"),
                        "session_id": projection.get("session_id"),
                        "appended_message_count": projection.get(
                            "appended_message_count"
                        ),
                        "divergence": projection.get("divergence"),
                    },
                    "result": _safe_item_result(result),
                    "completed_at": _now(),
                    "updated_at": _now(),
                },
                "$unset": {
                    "lease_token": "",
                    "lease_expires_at": "",
                    "next_retry_at": "",
                },
            },
        )
    except Exception as exc:
        retryable = isinstance(
            exc, (_RetryableItemError, OSError, TimeoutError, ConnectionError)
        ) or is_transient_mongo_error(exc)
        if isinstance(exc, ExternalConversationImportError):
            retryable = (
                exc.error_code == "external_conversation_source_changed_during_import"
            )
        attempts = int(item.get("attempt_count") or 1)
        retryable = retryable and attempts < MAX_ATTEMPTS
        status = (
            "retry_wait"
            if retryable
            else (
                "unsupported"
                if isinstance(exc, ExternalConversationImportError)
                else "failed"
            )
        )
        set_fields: dict[str, Any] = {
            "status": status,
            "error_code": getattr(exc, "error_code", type(exc).__name__),
            "error_message": str(exc)[:500],
            "updated_at": _now(),
        }
        if retryable:
            set_fields["next_retry_at"] = _now() + timedelta(
                seconds=min(300, 2**attempts)
            )
        else:
            set_fields["completed_at"] = _now()
        items.update_one(
            {
                "item_id": item["item_id"],
                "lease_token": item["lease_token"],
                "status": "running",
            },
            {"$set": set_fields, "$unset": {"lease_token": "", "lease_expires_at": ""}},
        )
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1)
        _settle_batch(str(item["batch_id"]))


def recover_external_conversation_imports() -> None:
    from .computer_file_copy_service import cleanup_stale_file_copy_spools

    removed_spools = cleanup_stale_file_copy_spools()
    if removed_spools:
        logger.info("Removed %d stale interrupted file-copy spool(s).", removed_spools)
    batches, _items = _collections()
    for batch in batches.find({"status": "planning"}):
        try:
            _populate_batch(batch)
        except Exception:
            logger.exception("Could not recover external conversation import planning")
    _WORKER_WAKE.set()


def _worker_loop() -> None:
    try:
        recover_external_conversation_imports()
    except Exception:
        logger.exception("External conversation import startup recovery was deferred")
    while not _WORKER_STOP.is_set():
        try:
            item = _claim_item()
            if item is not None:
                _process_item(item)
                continue
        except Exception:
            logger.exception(
                "External conversation bulk import worker iteration failed"
            )
        _WORKER_WAKE.wait(timeout=2.0)
        _WORKER_WAKE.clear()


def start_external_conversation_bulk_import_worker() -> threading.Thread | None:
    global _WORKER_THREAD
    if os.getenv(
        "VON_EXTERNAL_CONVERSATION_IMPORT_WORKER_ENABLE", "1"
    ).strip().lower() in {"0", "false", "no", "off"}:
        return None
    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return _WORKER_THREAD
        _WORKER_STOP.clear()
        _WORKER_THREAD = threading.Thread(
            target=_worker_loop,
            name="external-conversation-import-worker",
            daemon=True,
        )
        _WORKER_THREAD.start()
        return _WORKER_THREAD


def stop_external_conversation_bulk_import_worker() -> None:
    _WORKER_STOP.set()
    _WORKER_WAKE.set()
