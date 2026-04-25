from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .blob_store import BlobRef
from .blob_uploads import BlobUploadError, put_bytes_durable


DEBUG_PAYLOAD_BLOB_REF_SCHEMA_VERSION = "debug_payload_blob_ref.v1"
DEBUG_PAYLOAD_OFFLOAD_DEGRADED_SCHEMA_VERSION = "debug_payload_offload_degraded.v1"

DEFAULT_DEBUG_PAYLOAD_THRESHOLD_BYTES = 32 * 1024
DEFAULT_TOOL_MESSAGE_THRESHOLD_BYTES = 4 * 1024

_OFFLOAD_FIELD_NAMES = {
    "aux_llm_calls",
    "context_messages",
    "diagnostic_events",
    "latest_progress",
    "llm_interaction",
    "llm_request",
    "messages",
    "progress_events",
    "selected_workflow_trace",
    "stage_diagnostics",
    "tool_history",
    "turn_execution_diagnostics",
    "workflow_discovery",
    "workflow_routing",
    "workflow_routing_diagnostics",
}

_OFFLOAD_STRING_FIELD_NAMES = {
    "content",
    "detail",
    "detail_html",
    "payload",
    "raw",
    "response",
    "text",
}


@dataclass(frozen=True)
class DebugPayloadOffloadResult:
    payload: Any
    offloaded_count: int = 0
    degraded_count: int = 0
    original_size_bytes: int = 0
    stored_size_bytes: int = 0


@dataclass(frozen=True)
class DebugPayloadHydrationResult:
    payload: Any
    hydrated_count: int = 0
    error_count: int = 0


def _env_int(name: str, *, default: int) -> int:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except Exception:
        return default
    return parsed if parsed > 0 else default


def default_debug_payload_threshold_bytes() -> int:
    return _env_int(
        "VON_DEBUG_PAYLOAD_BLOB_THRESHOLD_BYTES",
        default=DEFAULT_DEBUG_PAYLOAD_THRESHOLD_BYTES,
    )


def default_tool_message_threshold_bytes() -> int:
    return _env_int(
        "VON_DEBUG_TOOL_MESSAGE_BLOB_THRESHOLD_BYTES",
        default=DEFAULT_TOOL_MESSAGE_THRESHOLD_BYTES,
    )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def estimate_payload_size_bytes(value: Any) -> int:
    return len(_json_bytes(value))


def debug_payload_sha256(value: Any) -> str:
    """Return the canonical JSON SHA-256 used for debug blob verification."""

    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_slug(value: Any, *, fallback: str = "unknown", limit: int = 96) -> str:
    text = str(value or "").strip()
    if not text:
        text = fallback
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    if not text:
        text = fallback
    return text[:limit]


def _namespace_hash(namespace: str | None) -> str:
    cleaned = namespace.strip() if isinstance(namespace, str) else ""
    if not cleaned:
        cleaned = "unknown_namespace"
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


def _blob_ref_to_payload(ref: BlobRef) -> dict[str, Any]:
    return {
        "backend": ref.backend,
        "key": ref.key,
        "uri": ref.uri,
        "content_type": ref.content_type,
        "size_bytes": ref.size_bytes,
        "etag": ref.etag,
        "metadata": dict(ref.metadata or {}),
    }


def _summarise_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        keys = [str(key) for key in list(value.keys())[:20]]
        return {"type": "object", "key_count": len(value), "keys": keys}
    if isinstance(value, list):
        return {
            "type": "array",
            "item_count": len(value),
            "item_types": sorted({type(item).__name__ for item in value[:20]}),
        }
    if isinstance(value, str):
        preview = value[:500]
        return {
            "type": "string",
            "char_count": len(value),
            "preview": preview,
            "preview_truncated": len(value) > len(preview),
        }
    return {"type": type(value).__name__}


def _is_blob_ref_payload(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema_version") == DEBUG_PAYLOAD_BLOB_REF_SCHEMA_VERSION
        and isinstance(value.get("blob_ref"), Mapping)
    )


def is_debug_payload_blob_ref(value: Any) -> bool:
    """Return True when *value* is a compact debug payload blob reference."""

    return _is_blob_ref_payload(value)


def load_debug_payload_blob_ref(
    ref_payload: Mapping[str, Any],
    *,
    expected_raw_sha256: str | None = None,
) -> Any:
    """Load and decode a debug payload blob reference.

    This is a reusable support surface for debug viewers, migration checks, and
    tests. It does not decide which fields should be offloaded.
    """

    if not _is_blob_ref_payload(ref_payload):
        raise ValueError("Not a debug payload blob reference")

    blob_ref = ref_payload.get("blob_ref")
    if not isinstance(blob_ref, Mapping):
        raise ValueError("Debug payload blob reference is missing blob_ref")
    key = blob_ref.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Debug payload blob reference is missing blob key")

    from .blob_store import get_blob_store_from_env

    compressed = get_blob_store_from_env().get_bytes(key.strip())
    if ref_payload.get("compression") == "gzip":
        raw_bytes = gzip.decompress(compressed)
    else:
        raw_bytes = compressed

    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    expected = expected_raw_sha256 or ref_payload.get("raw_sha256")
    if (
        isinstance(expected, str)
        and expected.strip()
        and expected.strip() != raw_sha256
    ):
        raise ValueError("Debug payload blob raw SHA-256 mismatch")

    return json.loads(raw_bytes.decode("utf-8"))


def hydrate_debug_payload_blob_refs(
    payload: Any,
    *,
    fail_soft: bool = True,
) -> DebugPayloadHydrationResult:
    """Recursively replace compact debug blob refs with their original values.

    Diagnostic readers use this to preserve the historical "exact debug payload"
    contract while allowing MongoDB to store only compact references.  When
    ``fail_soft`` is true, an unavailable blob leaves a small error marker next
    to the original reference rather than failing the entire debug view.
    """

    hydrated_count = 0
    error_count = 0

    def _walk(value: Any) -> Any:
        nonlocal hydrated_count, error_count

        if _is_blob_ref_payload(value):
            try:
                hydrated = load_debug_payload_blob_ref(value)
            except Exception as exc:
                error_count += 1
                if not fail_soft:
                    raise
                replacement = dict(value)
                replacement["hydration_error"] = {
                    "schema_version": "debug_payload_blob_hydration_error.v1",
                    "error": str(exc),
                    "error_class": type(exc).__name__,
                    "created_at_utc": _utcnow_iso(),
                }
                return replacement
            hydrated_count += 1
            return hydrated

        if isinstance(value, Mapping):
            return {str(key): _walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_walk(item) for item in value]
        return value

    hydrated_payload = _walk(payload)
    return DebugPayloadHydrationResult(
        payload=hydrated_payload,
        hydrated_count=hydrated_count,
        error_count=error_count,
    )


def _should_offload(
    *,
    path: tuple[str, ...],
    value: Any,
    size_bytes: int,
    threshold_bytes: int,
) -> bool:
    if size_bytes <= threshold_bytes or not path:
        return False

    key = path[-1].lower()
    if key in _OFFLOAD_FIELD_NAMES:
        return True
    if isinstance(value, str) and key in _OFFLOAD_STRING_FIELD_NAMES:
        return True

    # Protect Mongo from unexpected giant leaves even when the field was not
    # known when this support surface was authored.
    return size_bytes > max(threshold_bytes * 8, 512 * 1024)


def _offload_value(
    *,
    value: Any,
    root_kind: str,
    field_path: tuple[str, ...],
    namespace: str | None,
    request_id: str | None,
    fail_soft: bool,
) -> tuple[Any, bool, bool, int, int]:
    raw_bytes = _json_bytes(value)
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    compressed = gzip.compress(raw_bytes, compresslevel=6)
    compressed_sha256 = hashlib.sha256(compressed).hexdigest()

    field_path_text = ".".join(field_path)
    request_slug = _safe_slug(request_id, fallback="no_request_id")
    root_slug = _safe_slug(root_kind, fallback="debug_payload")
    field_slug = _safe_slug(field_path_text, fallback="payload", limit=160)
    key = (
        f"debug/turns/{_namespace_hash(namespace)}/{request_slug}/"
        f"{root_slug}/{field_slug}.{compressed_sha256}.json.gz"
    )
    metadata = {
        "schema_version": DEBUG_PAYLOAD_BLOB_REF_SCHEMA_VERSION,
        "payload_kind": root_kind,
        "field_path": field_path_text,
        "request_id": request_id,
        "namespace_hash": _namespace_hash(namespace),
        "raw_sha256": raw_sha256,
        "raw_size_bytes": str(len(raw_bytes)),
        "compression": "gzip",
        "content_encoding": "utf-8",
    }

    try:
        stored = put_bytes_durable(
            key=key,
            data=compressed,
            content_type="application/json",
            metadata=metadata,
            sha256=compressed_sha256,
            size_bytes=len(compressed),
        )
    except BlobUploadError as exc:
        if not fail_soft:
            raise
        return (
            {
                "schema_version": DEBUG_PAYLOAD_OFFLOAD_DEGRADED_SCHEMA_VERSION,
                "offloaded": False,
                "degraded": True,
                "reason": "blob_upload_failed",
                "error": str(exc),
                "payload_kind": root_kind,
                "field_path": field_path_text,
                "original_size_bytes": len(raw_bytes),
                "summary": _summarise_value(value),
                "created_at_utc": _utcnow_iso(),
            },
            False,
            True,
            len(raw_bytes),
            estimate_payload_size_bytes(_summarise_value(value)),
        )

    ref_payload = {
        "schema_version": DEBUG_PAYLOAD_BLOB_REF_SCHEMA_VERSION,
        "offloaded": True,
        "payload_kind": root_kind,
        "field_path": field_path_text,
        "content_type": "application/json",
        "compression": "gzip",
        "content_encoding": "utf-8",
        "original_size_bytes": len(raw_bytes),
        "stored_size_bytes": stored.size_bytes,
        "raw_sha256": raw_sha256,
        "sha256": stored.sha256,
        "summary": _summarise_value(value),
        "blob_ref": _blob_ref_to_payload(stored.ref),
        "created_at_utc": _utcnow_iso(),
    }
    return (
        ref_payload,
        True,
        False,
        len(raw_bytes),
        estimate_payload_size_bytes(ref_payload),
    )


def compact_debug_payload_for_storage(
    payload: Any,
    *,
    root_kind: str,
    namespace: str | None = None,
    request_id: str | None = None,
    threshold_bytes: int | None = None,
    fail_soft: bool = True,
) -> DebugPayloadOffloadResult:
    """Replace oversized debug leaves with durable blob references.

    The function is deliberately storage-oriented: it preserves compact counters
    and previews inline, while moving large low-query forensic payloads out of
    MongoDB. It never decides what diagnostic policy should exist.
    """

    threshold = (
        int(threshold_bytes)
        if isinstance(threshold_bytes, int) and threshold_bytes > 0
        else default_debug_payload_threshold_bytes()
    )
    original_size = estimate_payload_size_bytes(payload)
    offloaded_count = 0
    degraded_count = 0

    def _walk(value: Any, path: tuple[str, ...]) -> Any:
        nonlocal offloaded_count, degraded_count

        if _is_blob_ref_payload(value):
            return dict(value)

        size = estimate_payload_size_bytes(value)
        if _should_offload(
            path=path,
            value=value,
            size_bytes=size,
            threshold_bytes=threshold,
        ):
            replacement, offloaded, degraded, _raw_size, _stored_size = _offload_value(
                value=value,
                root_kind=root_kind,
                field_path=path,
                namespace=namespace,
                request_id=request_id,
                fail_soft=fail_soft,
            )
            offloaded_count += 1 if offloaded else 0
            degraded_count += 1 if degraded else 0
            return replacement

        if isinstance(value, Mapping):
            return {
                str(key): _walk(item, (*path, str(key))) for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                _walk(item, (*path, str(index))) for index, item in enumerate(value)
            ]
        return value

    compacted = _walk(payload, ())
    return DebugPayloadOffloadResult(
        payload=compacted,
        offloaded_count=offloaded_count,
        degraded_count=degraded_count,
        original_size_bytes=original_size,
        stored_size_bytes=estimate_payload_size_bytes(compacted),
    )
