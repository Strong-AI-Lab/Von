"""Change-aware freshness receipts for bounded startup seed materialisations.

The receipts in this module are derived support state.  They never replace
Vontology as authority: a missing, incompatible, or unresumable receipt is a
cache miss and callers retain their canonical verification path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from bson import BSON

STARTUP_SEED_FRESHNESS_RECEIPT_SCHEMA_VERSION = "startup_seed_freshness_receipt.v1"
_RECEIPT_FILE_SCHEMA_VERSION = "startup_seed_freshness_receipts.v1"
_DEFAULT_RECEIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "startup_seed_cache"
    / "freshness_receipts.json"
)
_RECEIPT_LOCK = threading.Lock()
_WATCHED_COLLECTIONS = ("concepts", "text_relations", "text_values")
_DDL_OPERATION_TYPES = frozenset({"drop", "rename", "dropDatabase", "invalidate"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalised_strings(values: Sequence[Any] | None) -> list[str]:
    return sorted({_text(value) for value in values or () if _text(value)})


def _receipt_path() -> Path:
    override = os.getenv("VON_STARTUP_SEED_FRESHNESS_RECEIPT_PATH")
    if isinstance(override, str) and override.strip():
        return Path(override.strip())
    return _DEFAULT_RECEIPT_PATH


def _receipts_enabled() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv(
        "VON_STARTUP_SEED_FRESHNESS_RECEIPT_PATH"
    ):
        return False
    return os.getenv(
        "VON_STARTUP_SEED_FRESHNESS_RECEIPTS_ENABLED", "1"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _max_await_time_ms() -> int:
    try:
        configured = int(
            os.getenv("VON_STARTUP_SEED_CHANGE_STREAM_MAX_AWAIT_MS", "100")
        )
    except (TypeError, ValueError):
        configured = 100
    return max(10, min(configured, 5_000))


def _max_events() -> int:
    try:
        configured = int(
            os.getenv("VON_STARTUP_SEED_CHANGE_STREAM_MAX_EVENTS", "10000")
        )
    except (TypeError, ValueError):
        configured = 10_000
    return max(1, min(configured, 1_000_000))


def _dependency_scope(
    *,
    concept_ids: Sequence[str],
    text_relation_subject_ids: Sequence[str],
    text_relation_predicates: Sequence[str],
) -> dict[str, list[str]]:
    return {
        "concept_ids": _normalised_strings(concept_ids),
        "text_relation_subject_ids": _normalised_strings(text_relation_subject_ids),
        "text_relation_predicates": _normalised_strings(text_relation_predicates),
    }


def build_startup_seed_source_digest(
    *,
    asset_path: str | Path,
    producer_paths: Sequence[str | Path],
    material_configuration: Mapping[str, Any] | None = None,
) -> str:
    """Digest the exact seed, producer code, and material configuration."""

    digest = hashlib.sha256()
    paths = [Path(asset_path), *[Path(path) for path in producer_paths]]
    for path in paths:
        resolved = path.resolve()
        digest.update(resolved.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(resolved.read_bytes())
        digest.update(b"\0")
    encoded_configuration = json.dumps(
        dict(material_configuration or {}),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest.update(encoded_configuration)
    return digest.hexdigest()


def _mongo_principal_fingerprint(uri: str) -> str | None:
    try:
        parsed = urlsplit(uri)
        query_options = {
            _text(key).lower(): _text(value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        }
        identity = {
            "username": unquote(parsed.username or "").strip(),
            "auth_source": query_options.get("authsource", ""),
            "auth_mechanism": query_options.get("authmechanism", ""),
        }
        if not any(identity.values()):
            return None
        return hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    except Exception:  # noqa: BLE001 - an unavailable identity is a cache miss
        return None


def _current_authority_fingerprint(db: Any) -> str:
    """Return a secret-free identity for the represented read authority."""

    from ..db.mongo_client import (
        MONGO_URI,
        get_effective_mongo_uri,
        get_mongo_fallback_policy_state,
        is_using_fallback_uri,
    )
    from ..db.mongo_uri_redaction import build_safe_mongo_connection_location

    effective_uri = get_effective_mongo_uri()
    fallback_policy = get_mongo_fallback_policy_state()
    identity = {
        "database_name": _text(getattr(db, "name", "")),
        "mongo_location": build_safe_mongo_connection_location(
            effective_uri,
            using_fallback=is_using_fallback_uri(),
            fallback_kind=fallback_policy.get("active_fallback_kind"),
            fallback_target_uri=MONGO_URI,
        ),
        "mongo_principal_fingerprint": _mongo_principal_fingerprint(effective_uri),
        "namespace": _text(os.getenv("VON_DEFAULT_NAMESPACE")),
    }
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _get_db() -> Any:
    from ..db.mongo_client import get_db

    return get_db()


def _encode_resume_token(token: Mapping[str, Any]) -> str:
    return base64.b64encode(BSON.encode({"resume_token": dict(token)})).decode("ascii")


def _decode_resume_token(encoded: Any) -> Mapping[str, Any] | None:
    if not isinstance(encoded, str) or not encoded.strip():
        return None
    try:
        payload = BSON(base64.b64decode(encoded.encode("ascii"))).decode()
    except Exception:  # noqa: BLE001 - malformed opaque tokens are cache misses
        return None
    token = payload.get("resume_token")
    return token if isinstance(token, Mapping) else None


def _read_receipt_entries(path: Path) -> dict[str, Mapping[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - corrupt/missing receipts are cache misses
        return {}
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != _RECEIPT_FILE_SCHEMA_VERSION
        or not isinstance(payload.get("entries"), Mapping)
    ):
        return {}
    return {
        _text(key): dict(value)
        for key, value in payload["entries"].items()
        if _text(key) and isinstance(value, Mapping)
    }


def _persist_receipt_entry(family_id: str, entry: Mapping[str, Any]) -> None:
    path = _receipt_path()
    temp_path: str | None = None
    with _RECEIPT_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = _read_receipt_entries(path)
        entries[family_id] = dict(entry)
        payload = {
            "schema_version": _RECEIPT_FILE_SCHEMA_VERSION,
            "entries": entries,
        }
        handle, temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(
                    payload,
                    stream,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass


def _public_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        _text(key): value
        for key, value in result.items()
        if not _text(key).startswith("_")
    }


def public_startup_seed_freshness_result(
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove opaque checkpoint values before exposing startup telemetry."""

    return _public_result(result)


def _normalised_tracked_ids(values: Sequence[Any] | None) -> list[str]:
    return _normalised_strings(values)


def _event_relevance(
    event: Mapping[str, Any],
    *,
    scope: Mapping[str, Sequence[str]],
    tracked_ids: Mapping[str, Sequence[str]],
) -> tuple[bool, bool]:
    """Return ``(relevant, determinate)`` for one database change event."""

    operation_type = _text(event.get("operationType"))
    if operation_type in _DDL_OPERATION_TYPES:
        return True, True

    namespace = event.get("ns")
    collection_name = (
        _text(namespace.get("coll")) if isinstance(namespace, Mapping) else ""
    )
    if collection_name not in _WATCHED_COLLECTIONS:
        return False, True

    document_key = event.get("documentKey")
    raw_document_id = (
        document_key.get("_id") if isinstance(document_key, Mapping) else None
    )
    document_id = _text(raw_document_id)
    full_document = event.get("fullDocument")
    document = full_document if isinstance(full_document, Mapping) else {}

    if collection_name == "concepts":
        if document_id and document_id in set(
            tracked_ids.get("concept_document_ids") or ()
        ):
            return True, True
        concept_id = _text(document.get("concept_id"))
        if concept_id:
            return concept_id in set(scope.get("concept_ids") or ()), True
    elif collection_name == "text_relations":
        if document_id and document_id in set(
            tracked_ids.get("text_relation_document_ids") or ()
        ):
            return True, True
        subject_id = _text(document.get("subject_concept_id"))
        predicate = _text(document.get("predicate"))
        if subject_id or predicate:
            return (
                subject_id in set(scope.get("text_relation_subject_ids") or ())
                and predicate in set(scope.get("text_relation_predicates") or ())
            ), True
    elif collection_name == "text_values":
        if document_id:
            return (
                document_id in set(tracked_ids.get("text_value_document_ids") or ()),
                True,
            )

    if operation_type == "delete" and document_id:
        # An untracked deletion cannot affect the exact verified dependency set.
        return False, True
    return False, False


def _watch_pipeline() -> list[dict[str, Any]]:
    return [
        {
            "$match": {
                "$or": [
                    {"ns.coll": {"$in": list(_WATCHED_COLLECTIONS)}},
                    {"operationType": {"$in": sorted(_DDL_OPERATION_TYPES)}},
                ]
            }
        }
    ]


def _drain_changes(
    *,
    db: Any,
    observation: Mapping[str, Any],
    scope: Mapping[str, Sequence[str]],
    tracked_ids: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    watch_options: dict[str, Any] = {
        "full_document": "updateLookup",
        "max_await_time_ms": _max_await_time_ms(),
    }
    resume_token = observation.get("_resume_token")
    start_at_operation_time = observation.get("_start_at_operation_time")
    if isinstance(resume_token, Mapping):
        watch_options["resume_after"] = dict(resume_token)
    elif start_at_operation_time is not None:
        watch_options["start_at_operation_time"] = start_at_operation_time
    else:
        return {
            "success": False,
            "reason": "change_stream_observation_missing",
            "events_examined": 0,
            "relevant_events": 0,
        }

    events_examined = 0
    relevant_events = 0
    indeterminate_events = 0
    stream = None
    try:
        stream = db.watch(_watch_pipeline(), **watch_options)
        while True:
            event = stream.try_next()
            if event is None:
                break
            events_examined += 1
            relevant, determinate = _event_relevance(
                event,
                scope=scope,
                tracked_ids=tracked_ids,
            )
            if relevant:
                relevant_events += 1
                break
            if not determinate:
                indeterminate_events += 1
                break
            if events_examined >= _max_events():
                return {
                    "success": False,
                    "reason": "change_stream_event_limit_reached",
                    "events_examined": events_examined,
                    "relevant_events": relevant_events,
                    "indeterminate_events": indeterminate_events,
                }
        current_token = getattr(stream, "resume_token", None)
        if relevant_events:
            return {
                "success": True,
                "fresh": False,
                "reason": "relevant_dependency_change_observed",
                "events_examined": events_examined,
                "relevant_events": relevant_events,
                "indeterminate_events": indeterminate_events,
            }
        if indeterminate_events:
            return {
                "success": False,
                "fresh": False,
                "reason": "change_stream_event_indeterminate",
                "events_examined": events_examined,
                "relevant_events": 0,
                "indeterminate_events": indeterminate_events,
            }
        if not isinstance(current_token, Mapping):
            return {
                "success": False,
                "fresh": False,
                "reason": "change_stream_resume_token_missing",
                "events_examined": events_examined,
                "relevant_events": 0,
                "indeterminate_events": 0,
            }
        return {
            "success": True,
            "fresh": True,
            "reason": "no_relevant_dependency_change",
            "events_examined": events_examined,
            "relevant_events": 0,
            "indeterminate_events": 0,
            "_resume_token": dict(current_token),
        }
    except Exception as exc:  # noqa: BLE001 - any driver failure must fail closed
        return {
            "success": False,
            "fresh": False,
            "reason": "change_stream_unavailable_or_unresumable",
            "error_type": type(exc).__name__,
            "events_examined": events_examined,
            "relevant_events": relevant_events,
            "indeterminate_events": indeterminate_events,
        }
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001,S110 - close is best-effort only
                pass


def begin_startup_seed_freshness_observation() -> dict[str, Any]:
    """Capture a server operation time before canonical verification begins."""

    if not _receipts_enabled():
        return {"success": False, "reason": "freshness_receipts_disabled"}
    try:
        db = _get_db()
        hello = db.command("hello")
        operation_time = hello.get("operationTime")
        if operation_time is None:
            return {
                "success": False,
                "reason": "mongo_operation_time_unavailable",
            }
        return {
            "success": True,
            "reason": "mongo_operation_time_captured",
            "_start_at_operation_time": operation_time,
        }
    except Exception as exc:  # noqa: BLE001 - unavailable observation is typed
        return {
            "success": False,
            "reason": "mongo_operation_time_capture_failed",
            "error_type": type(exc).__name__,
        }


def check_startup_seed_freshness(
    *,
    family_id: str,
    source_digest: str,
    producer_schema_version: str,
    concept_ids: Sequence[str],
    text_relation_subject_ids: Sequence[str],
    text_relation_predicates: Sequence[str],
) -> dict[str, Any]:
    """Validate a receipt and drain its checkpoint to the current high-water mark."""

    started_at = time.perf_counter()
    base_result: dict[str, Any] = {
        "fresh": False,
        "family_id": family_id,
        "receipt_schema_version": STARTUP_SEED_FRESHNESS_RECEIPT_SCHEMA_VERSION,
    }
    if not _receipts_enabled():
        return {
            **base_result,
            "reason": "freshness_receipts_disabled",
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    scope = _dependency_scope(
        concept_ids=concept_ids,
        text_relation_subject_ids=text_relation_subject_ids,
        text_relation_predicates=text_relation_predicates,
    )
    entry = _read_receipt_entries(_receipt_path()).get(family_id)
    if not isinstance(entry, Mapping):
        return {
            **base_result,
            "reason": "receipt_missing",
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    if entry.get("schema_version") != STARTUP_SEED_FRESHNESS_RECEIPT_SCHEMA_VERSION:
        return {
            **base_result,
            "reason": "receipt_schema_mismatch",
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    if entry.get("source_digest") != source_digest:
        return {
            **base_result,
            "reason": "source_digest_mismatch",
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    if entry.get("producer_schema_version") != producer_schema_version:
        return {
            **base_result,
            "reason": "producer_schema_mismatch",
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    if entry.get("dependency_scope") != scope:
        return {
            **base_result,
            "reason": "dependency_scope_mismatch",
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    tracked_ids = entry.get("tracked_ids")
    if not isinstance(tracked_ids, Mapping):
        return {
            **base_result,
            "reason": "tracked_dependency_ids_missing",
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    token = _decode_resume_token(entry.get("resume_token_bson_base64"))
    if token is None:
        return {
            **base_result,
            "reason": "resume_token_invalid",
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    try:
        db = _get_db()
        authority_fingerprint = _current_authority_fingerprint(db)
    except Exception as exc:  # noqa: BLE001 - authority uncertainty is a miss
        return {
            **base_result,
            "reason": "authority_fingerprint_unavailable",
            "error_type": type(exc).__name__,
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    if entry.get("authority_fingerprint") != authority_fingerprint:
        return {
            **base_result,
            "reason": "authority_fingerprint_mismatch",
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    drained = _drain_changes(
        db=db,
        observation={"_resume_token": token},
        scope=scope,
        tracked_ids=tracked_ids,
    )
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    if not drained.get("fresh"):
        return {
            **base_result,
            **_public_result(drained),
            "duration_ms": duration_ms,
        }

    current_token = drained.get("_resume_token")
    if not isinstance(current_token, Mapping):
        return {
            **base_result,
            "reason": "change_stream_resume_token_missing",
            "duration_ms": duration_ms,
        }
    refreshed_entry = {
        **dict(entry),
        "resume_token_bson_base64": _encode_resume_token(current_token),
        "last_checked_at_epoch_seconds": time.time(),
    }
    try:
        _persist_receipt_entry(family_id, refreshed_entry)
    except Exception as exc:  # noqa: BLE001 - persistence failure is a miss
        return {
            **base_result,
            "reason": "receipt_checkpoint_persist_failed",
            "error_type": type(exc).__name__,
            "events_examined": drained.get("events_examined", 0),
            "duration_ms": duration_ms,
        }
    return {
        **base_result,
        "fresh": True,
        "reason": "dependency_receipt_current",
        "events_examined": drained.get("events_examined", 0),
        "relevant_events": 0,
        "indeterminate_events": 0,
        "verified_at_epoch_seconds": entry.get("verified_at_epoch_seconds"),
        "metadata": dict(entry.get("metadata") or {}),
        "duration_ms": duration_ms,
    }


def record_startup_seed_freshness(
    *,
    family_id: str,
    source_digest: str,
    producer_schema_version: str,
    concept_ids: Sequence[str],
    text_relation_subject_ids: Sequence[str],
    text_relation_predicates: Sequence[str],
    tracked_concept_document_ids: Sequence[Any],
    tracked_text_relation_document_ids: Sequence[Any],
    tracked_text_value_document_ids: Sequence[Any],
    observation: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish a receipt only if canonical verification had a quiet change window."""

    started_at = time.perf_counter()
    base_result: dict[str, Any] = {"family_id": family_id, "persisted": False}
    if not observation.get("success"):
        return {
            **base_result,
            "reason": _text(observation.get("reason"))
            or "freshness_observation_unavailable",
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    scope = _dependency_scope(
        concept_ids=concept_ids,
        text_relation_subject_ids=text_relation_subject_ids,
        text_relation_predicates=text_relation_predicates,
    )
    tracked_ids = {
        "concept_document_ids": _normalised_tracked_ids(tracked_concept_document_ids),
        "text_relation_document_ids": _normalised_tracked_ids(
            tracked_text_relation_document_ids
        ),
        "text_value_document_ids": _normalised_tracked_ids(
            tracked_text_value_document_ids
        ),
    }
    try:
        db = _get_db()
        authority_fingerprint = _current_authority_fingerprint(db)
    except Exception as exc:  # noqa: BLE001 - authority uncertainty is a miss
        return {
            **base_result,
            "reason": "authority_fingerprint_unavailable",
            "error_type": type(exc).__name__,
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
    drained = _drain_changes(
        db=db,
        observation=observation,
        scope=scope,
        tracked_ids=tracked_ids,
    )
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    current_token = drained.get("_resume_token")
    if not drained.get("fresh") or not isinstance(current_token, Mapping):
        return {
            **base_result,
            **_public_result(drained),
            "duration_ms": duration_ms,
        }
    now = time.time()
    entry = {
        "schema_version": STARTUP_SEED_FRESHNESS_RECEIPT_SCHEMA_VERSION,
        "family_id": family_id,
        "source_digest": source_digest,
        "producer_schema_version": producer_schema_version,
        "authority_fingerprint": authority_fingerprint,
        "dependency_scope": scope,
        "tracked_ids": tracked_ids,
        "resume_token_bson_base64": _encode_resume_token(current_token),
        "verified_at_epoch_seconds": now,
        "last_checked_at_epoch_seconds": now,
        "metadata": dict(metadata or {}),
    }
    try:
        _persist_receipt_entry(family_id, entry)
    except Exception as exc:  # noqa: BLE001 - persistence failure is non-current
        return {
            **base_result,
            "reason": "receipt_persist_failed",
            "error_type": type(exc).__name__,
            "events_examined": drained.get("events_examined", 0),
            "duration_ms": duration_ms,
        }
    return {
        **base_result,
        "persisted": True,
        "reason": "dependency_receipt_persisted",
        "events_examined": drained.get("events_examined", 0),
        "relevant_events": 0,
        "indeterminate_events": 0,
        "tracked_dependency_counts": {
            key: len(value) for key, value in tracked_ids.items()
        },
        "duration_ms": duration_ms,
    }


__all__ = [
    "STARTUP_SEED_FRESHNESS_RECEIPT_SCHEMA_VERSION",
    "begin_startup_seed_freshness_observation",
    "build_startup_seed_source_digest",
    "check_startup_seed_freshness",
    "public_startup_seed_freshness_result",
    "record_startup_seed_freshness",
]
