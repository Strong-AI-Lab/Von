from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .blob_store import BlobRef
from .blob_uploads import BlobUploadError, put_bytes_durable


WORKFLOW_PAYLOAD_BLOB_REF_SCHEMA_VERSION = "workflow_payload_blob_ref.v1"
WORKFLOW_PAYLOAD_OFFLOAD_DEGRADED_SCHEMA_VERSION = (
    "workflow_payload_offload_degraded.v1"
)

DEFAULT_WORKFLOW_PAYLOAD_THRESHOLD_BYTES = 32 * 1024

_OFFLOAD_FIELD_NAMES = {
    "actions",
    "aux_llm_calls",
    "context_messages",
    "episode_critique_memory_upsert",
    "episode_evidence_bundle",
    "last_action_outputs",
    "last_failed_action_outputs",
    "last_workflow_step_result_envelope",
    "llm_calls",
    "llm_step_envelope",
    "messages",
    "observed_evidence",
    "rendered_prompt_variables",
    "state_transitions",
    "steps",
    "tool_invocations",
    "tool_messages",
    "turn_execution_outcome",
    "turn_execution_record",
    "turn_execution_runtime",
    "workflow_metadata_validation_events",
    "workflow_result_envelope",
    "workflow_step_result_envelopes",
    "workflow_tool_output_mapping_events",
}

_OFFLOAD_STRING_FIELD_NAMES = {
    "body",
    "content",
    "document_text",
    "final_response",
    "html",
    "markdown",
    "prompt",
    "raw",
    "raw_response",
    "raw_text",
    "response",
    "text",
}


@dataclass(frozen=True)
class WorkflowPayloadOffloadResult:
    payload: Any
    offloaded_count: int = 0
    degraded_count: int = 0
    original_size_bytes: int = 0
    stored_size_bytes: int = 0


@dataclass(frozen=True)
class WorkflowPayloadHydrationResult:
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


def default_workflow_payload_threshold_bytes() -> int:
    return _env_int(
        "VON_WORKFLOW_PAYLOAD_BLOB_THRESHOLD_BYTES",
        default=DEFAULT_WORKFLOW_PAYLOAD_THRESHOLD_BYTES,
    )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def estimate_workflow_payload_size_bytes(value: Any) -> int:
    return len(_json_bytes(value))


def workflow_payload_sha256(value: Any) -> str:
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
        return {
            "type": "string",
            "char_count": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(),
        }
    return {"type": type(value).__name__}


def _is_blob_ref_payload(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema_version") == WORKFLOW_PAYLOAD_BLOB_REF_SCHEMA_VERSION
        and isinstance(value.get("blob_ref"), Mapping)
    )


def is_workflow_payload_blob_ref(value: Any) -> bool:
    return _is_blob_ref_payload(value)


def load_workflow_payload_blob_ref(
    ref_payload: Mapping[str, Any],
    *,
    expected_raw_sha256: str | None = None,
) -> Any:
    if not _is_blob_ref_payload(ref_payload):
        raise ValueError("Not a workflow payload blob reference")

    blob_ref = ref_payload.get("blob_ref")
    if not isinstance(blob_ref, Mapping):
        raise ValueError("Workflow payload blob reference is missing blob_ref")
    key = blob_ref.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Workflow payload blob reference is missing blob key")

    from .blob_store import get_blob_store_for_backend_from_env, get_blob_store_from_env

    def _get_bytes_with_retries(store: Any, blob_key: str, *, attempts: int = 3) -> bytes:
        last_exc: Exception | None = None
        for _attempt in range(max(1, int(attempts))):
            try:
                return store.get_bytes(blob_key)
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Blob read failed without an exception")

    clean_key = key.strip()
    backend = blob_ref.get("backend")
    backend_text = backend.strip() if isinstance(backend, str) else ""
    compressed: bytes | None = None
    first_error: Exception | None = None

    if backend_text == "spillway":
        try:
            from .blob_spillway import get_blob_spillway_queue

            compressed = get_blob_spillway_queue().get_local_bytes(clean_key)
        except Exception as exc:
            first_error = exc

    if compressed is None:
        try:
            store = get_blob_store_from_env()
            compressed = _get_bytes_with_retries(store, clean_key, attempts=2)
        except Exception as exc:
            first_error = first_error or exc

    if compressed is None and backend_text and backend_text != "spillway":
        try:
            compressed = _get_bytes_with_retries(
                get_blob_store_for_backend_from_env(backend_text),
                clean_key,
                attempts=3,
            )
        except Exception as exc:
            first_error = first_error or exc

    if compressed is None:
        if first_error is not None:
            raise first_error
        raise RuntimeError("Workflow payload blob read failed")
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
        raise ValueError("Workflow payload blob raw SHA-256 mismatch")

    return json.loads(raw_bytes.decode("utf-8"))


def hydrate_workflow_payload_blob_refs(
    payload: Any,
    *,
    fail_soft: bool = True,
) -> WorkflowPayloadHydrationResult:
    hydrated_count = 0
    error_count = 0

    def _walk(value: Any) -> Any:
        nonlocal hydrated_count, error_count

        if _is_blob_ref_payload(value):
            try:
                hydrated = load_workflow_payload_blob_ref(value)
            except Exception as exc:
                error_count += 1
                if not fail_soft:
                    raise
                replacement = dict(value)
                replacement["hydration_error"] = {
                    "schema_version": "workflow_payload_blob_hydration_error.v1",
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

    return WorkflowPayloadHydrationResult(
        payload=_walk(payload),
        hydrated_count=hydrated_count,
        error_count=error_count,
    )


def _semantic_path_key(path: tuple[str, ...]) -> str:
    for part in reversed(path):
        if not str(part).isdigit():
            return str(part).lower()
    return ""


def _should_offload(
    *,
    path: tuple[str, ...],
    value: Any,
    size_bytes: int,
    threshold_bytes: int,
) -> bool:
    if size_bytes <= threshold_bytes or not path:
        return False

    key = _semantic_path_key(path)
    if key in _OFFLOAD_FIELD_NAMES:
        return True
    if isinstance(value, str) and key in _OFFLOAD_STRING_FIELD_NAMES:
        return True

    return size_bytes > max(threshold_bytes * 8, 256 * 1024)


def count_workflow_payload_offload_candidates(
    payload: Any,
    *,
    threshold_bytes: int | None = None,
) -> int:
    threshold = (
        int(threshold_bytes)
        if isinstance(threshold_bytes, int) and threshold_bytes > 0
        else default_workflow_payload_threshold_bytes()
    )
    candidate_count = 0

    def _walk(value: Any, path: tuple[str, ...]) -> None:
        nonlocal candidate_count

        if _is_blob_ref_payload(value):
            return

        size = estimate_workflow_payload_size_bytes(value)
        if _should_offload(
            path=path,
            value=value,
            size_bytes=size,
            threshold_bytes=threshold,
        ):
            candidate_count += 1
            return

        if isinstance(value, Mapping):
            for key, item in value.items():
                _walk(item, (*path, str(key)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                _walk(item, (*path, str(index)))

    _walk(payload, ())
    return candidate_count


def _offload_value(
    *,
    value: Any,
    record_family: str,
    record_id: str | None,
    field_path: tuple[str, ...],
    namespace: str | None,
    workflow_id: str | None,
    fail_soft: bool,
) -> tuple[Any, bool, bool, int, int]:
    raw_bytes = _json_bytes(value)
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    compressed = gzip.compress(raw_bytes, compresslevel=6)
    compressed_sha256 = hashlib.sha256(compressed).hexdigest()

    field_path_text = ".".join(field_path)
    family_slug = _safe_slug(record_family, fallback="workflow_payload")
    record_slug = _safe_slug(record_id, fallback="no_record_id")
    field_slug = _safe_slug(field_path_text, fallback="payload", limit=160)
    key = (
        f"workflows/payloads/{_namespace_hash(namespace)}/{family_slug}/"
        f"{record_slug}/{field_slug}.{compressed_sha256}.{uuid.uuid4().hex}.json.gz"
    )
    metadata = {
        "schema_version": WORKFLOW_PAYLOAD_BLOB_REF_SCHEMA_VERSION,
        "payload_kind": record_family,
        "field_path": field_path_text,
        "record_id": record_id,
        "workflow_id": workflow_id,
        "namespace_hash": _namespace_hash(namespace),
        "raw_sha256": raw_sha256,
        "raw_size_bytes": str(len(raw_bytes)),
        "compression": "gzip",
        "content_encoding": "utf-8",
    }

    try:
        from .blob_spillway import is_spillway_enabled

        if is_spillway_enabled():
            from .blob_uploads import enqueue_bytes_via_spillway

            stored = enqueue_bytes_via_spillway(
                key=key,
                data=compressed,
                content_type="application/json",
                metadata=metadata,
                sha256=compressed_sha256,
                size_bytes=len(compressed),
            )
        else:
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
        summary = _summarise_value(value)
        return (
            {
                "schema_version": WORKFLOW_PAYLOAD_OFFLOAD_DEGRADED_SCHEMA_VERSION,
                "offloaded": False,
                "degraded": True,
                "reason": "blob_upload_failed",
                "error": str(exc),
                "payload_kind": record_family,
                "field_path": field_path_text,
                "original_size_bytes": len(raw_bytes),
                "summary": summary,
                "created_at_utc": _utcnow_iso(),
            },
            False,
            True,
            len(raw_bytes),
            estimate_workflow_payload_size_bytes(summary),
        )

    ref_payload = {
        "schema_version": WORKFLOW_PAYLOAD_BLOB_REF_SCHEMA_VERSION,
        "offloaded": True,
        "payload_kind": record_family,
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
        "persistence_status": (
            "queued_local_first" if stored.ref.backend == "spillway" else "committed"
        ),
        "remote_state": "pending" if stored.ref.backend == "spillway" else "committed",
        "created_at_utc": _utcnow_iso(),
    }
    return (
        ref_payload,
        True,
        False,
        len(raw_bytes),
        estimate_workflow_payload_size_bytes(ref_payload),
    )


def compact_workflow_payload_for_storage(
    payload: Any,
    *,
    record_family: str,
    record_id: str | None = None,
    namespace: str | None = None,
    workflow_id: str | None = None,
    threshold_bytes: int | None = None,
    fail_soft: bool = True,
) -> WorkflowPayloadOffloadResult:
    threshold = (
        int(threshold_bytes)
        if isinstance(threshold_bytes, int) and threshold_bytes > 0
        else default_workflow_payload_threshold_bytes()
    )
    original_size = estimate_workflow_payload_size_bytes(payload)
    offloaded_count = 0
    degraded_count = 0

    def _walk(value: Any, path: tuple[str, ...]) -> Any:
        nonlocal offloaded_count, degraded_count

        if _is_blob_ref_payload(value):
            return dict(value)

        size = estimate_workflow_payload_size_bytes(value)
        if _should_offload(
            path=path,
            value=value,
            size_bytes=size,
            threshold_bytes=threshold,
        ):
            replacement, offloaded, degraded, _raw_size, _stored_size = _offload_value(
                value=value,
                record_family=record_family,
                record_id=record_id,
                field_path=path,
                namespace=namespace,
                workflow_id=workflow_id,
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
    return WorkflowPayloadOffloadResult(
        payload=compacted,
        offloaded_count=offloaded_count,
        degraded_count=degraded_count,
        original_size_bytes=original_size,
        stored_size_bytes=estimate_workflow_payload_size_bytes(compacted),
    )
